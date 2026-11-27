"""Skill 管理端包导入、在线 Markdown CRUD、可见性治理、执行审计与组合门面。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from pathlib import Path

from app.access import AccessPolicyService
from app.auth.guard import require_session_roles
from app.skill.dependency import DependencyChecker
from app.skill.package import SkillPackageValidator, ValidatedSkillPackage
from app.skill.package_cache import SkillPackageCache
from app.skill.personal import (
    PersonalSkillDraft,
    build_admin_markdown_slug,
    build_markdown_manifest,
    parse_personal_markdown,
    validate_optional_admin_slug,
    validate_personal_content,
    validate_starter_prompt,
    validate_version_label,
)
from app.skill.personal_service import (
    PersonalSkillService,
    SkillUserAuthorizationError,
    commit_markdown_write,
    markdown_dependency_check,
    sha256_json,
    single_version_details,
    validate_idempotency_key,
)
from app.skill.results import SkillAdminDetails, SkillResourceSlice
from app.skill.runtime_reader import (
    SkillRuntimeReader,
    check_dependencies,
    is_plain_markdown_skill,
    require_object_store,
)
from app.skill.runtime_reader import (
    _decode_utf8_resource_slice as _decode_utf8_resource_slice,
)
from common.async_utils import run_thread_shielded
from common.date import utc_now
from common.ids import new_id
from common.unset import UNSET, UnsetType
from domain.access import (
    AccessPermissionDeniedError,
    InvalidVisibilityConfigError,
    ResourceAccessTarget,
    ResourceType,
    VisibilityGrantInput,
    VisibilityScope,
)
from domain.auth import AuthContext, CurrentSessionReader, User, UserRole
from domain.file import FileObjectStore
from domain.skill import (
    DependencyCheck,
    DependencyCheckStatus,
    RunSkillEntry,
    RunSkillPromptContext,
    RunSkillSnapshot,
    Skill,
    SkillConflictError,
    SkillDependencyUnsatisfiedError,
    SkillExecutionRecord,
    SkillFormat,
    SkillImportRequestResult,
    SkillNotFoundError,
    SkillPackageError,
    SkillRepository,
    SkillScope,
    SkillScriptDefinition,
    SkillVersion,
    SkillVersionPage,
)


class SkillAuthorizationError(PermissionError):
    """管理操作未通过事务外的当前管理员预检。"""


def _skill_access_target(skill: Skill) -> ResourceAccessTarget:
    """构造 Skill 的统一权限引擎判定目标。"""
    return ResourceAccessTarget(
        resource_type=ResourceType.SKILL,
        resource_id=skill.id,
        visibility_scope=VisibilityScope(skill.scope.value),
        owner_id=skill.owner_id,
        owner_department_id=skill.owner_department_id,
    )


class SkillService:
    """管理不可变包版本，按三层权限引擎分权管控，并组合个人与运行期协作者提供统一门面。"""

    def __init__(
        self,
        repository: SkillRepository,
        object_store: FileObjectStore | None,
        auth_reader: CurrentSessionReader,
        *,
        access_policy: AccessPolicyService,
        package_validator: SkillPackageValidator | None = None,
        dependency_checker: DependencyChecker | None = None,
        package_cache: SkillPackageCache | None = None,
        id_factory: Callable[[str], str] = new_id,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """保存持久化、可选对象存储（仅 ZIP/日志需要）、认证、权限引擎、依赖检查器与协作者。"""
        self._repository = repository
        self._object_store = object_store
        self._auth_reader = auth_reader
        self._access_policy = access_policy
        self._package_validator = package_validator or SkillPackageValidator()
        self._dependency_checker = dependency_checker or DependencyChecker(clock=clock)
        self._package_cache = package_cache
        self._id_factory = id_factory
        self._clock = clock
        self._personal = PersonalSkillService(
            repository,
            id_factory=id_factory,
            clock=clock,
        )
        self._runtime_reader = SkillRuntimeReader(
            repository,
            object_store,
            dependency_checker=self._dependency_checker,
            package_cache=package_cache,
            clock=clock,
        )

    def _require_object_store(self) -> FileObjectStore:
        """对象存储未启用时拒绝 ZIP 导入、ZIP 资源读取与脚本日志下载，统一映射 503。"""
        return require_object_store(self._object_store)

    async def list_admin(self, actor: AuthContext) -> tuple[Skill, ...]:
        """读取当前管理员可维护目录项（超管全量，部门管理员仅管辖部门）。"""
        user = await self._authorize_skill_manager(actor)
        skills = await self._repository.list_skills()
        if user.role is UserRole.SUPER_ADMIN:
            return skills
        snapshot = await self._access_policy.get_user_snapshot(user.id)
        if snapshot is None or not snapshot.is_active:
            return ()
        return tuple(
            skill
            for skill in skills
            if skill.scope is SkillScope.DEPARTMENT
            and skill.owner_department_id in snapshot.managed_department_ids
        )

    async def get_admin(self, actor: AuthContext, skill_id: str) -> SkillAdminDetails:
        """读取管理员详情和历史版本（不可读返回 404，可读但不可管理返回 403）。"""
        skill = await self._require_manageable_admin_skill(actor, skill_id)
        page = await self._repository.list_versions(skill_id, page=1, page_size=100)
        if page is None:
            raise SkillNotFoundError()
        return SkillAdminDetails(skill, page.items)

    # ------------------------------------------------------------------
    # 个人 Markdown Skill 组合门面（委托 PersonalSkillService）
    # ------------------------------------------------------------------

    async def list_personal(self, actor: AuthContext) -> tuple[Skill, ...]:
        """读取当前用户未删除的个人 Skill，包含停用项。"""
        return await self._personal.list_personal(actor)

    async def get_personal(
        self,
        actor: AuthContext,
        skill_id: str,
    ) -> SkillAdminDetails:
        """读取当前用户个人 Skill 详情及当前版本正文。"""
        return await self._personal.get_personal(actor, skill_id)

    async def parse_personal_document(
        self,
        filename: str,
        path: Path,
    ) -> PersonalSkillDraft:
        """在异步服务边界外执行有界 Markdown 文件解析。"""
        return await self._personal.parse_personal_document(filename, path)

    async def create_personal(
        self,
        actor: AuthContext,
        *,
        name: str,
        description: str,
        guide: str,
        idempotency_key: str,
    ) -> SkillAdminDetails:
        """创建并启用当前用户的首个个人 Skill。"""
        return await self._personal.create_personal(
            actor,
            name=name,
            description=description,
            guide=guide,
            idempotency_key=idempotency_key,
        )

    async def publish_personal(
        self,
        actor: AuthContext,
        skill_id: str,
        *,
        name: str,
        description: str,
        guide: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> SkillAdminDetails:
        """保存个人 Skill 新版本，并在同一事务内启用新版本。"""
        return await self._personal.publish_personal(
            actor,
            skill_id,
            name=name,
            description=description,
            guide=guide,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    async def set_personal_enabled(
        self,
        actor: AuthContext,
        skill_id: str,
        *,
        enabled: bool,
        expected_revision: int,
    ) -> SkillAdminDetails:
        """按修订号启停当前用户自己的个人 Skill。"""
        return await self._personal.set_personal_enabled(
            actor,
            skill_id,
            enabled=enabled,
            expected_revision=expected_revision,
        )

    async def delete_personal(
        self,
        actor: AuthContext,
        skill_id: str,
        *,
        expected_revision: int,
    ) -> None:
        """软删除当前用户自己的个人 Skill，历史版本保持不变。"""
        await self._personal.delete_personal(
            actor,
            skill_id,
            expected_revision=expected_revision,
        )

    # ------------------------------------------------------------------
    # 管理端版本、在线 Markdown、ZIP 导入与配置治理
    # ------------------------------------------------------------------

    async def get_version(self, actor: AuthContext, skill_id: str, version_id: str) -> SkillVersion:
        """读取目标 Skill 下不可变版本的完整正文。"""
        await self._require_manageable_admin_skill(actor, skill_id)
        version = await self._repository.get_version(version_id)
        if version is None or version.skill_id != skill_id:
            raise SkillNotFoundError("skill_version_not_found")
        return version

    async def list_versions(
        self,
        actor: AuthContext,
        skill_id: str,
        *,
        page: int,
        page_size: int,
    ) -> SkillVersionPage:
        """读取管理员历史版本分页。"""
        await self._require_manageable_admin_skill(actor, skill_id)
        result = await self._repository.list_versions(
            skill_id,
            page=page,
            page_size=page_size,
        )
        if result is None:
            raise SkillNotFoundError()
        return result

    async def parse_admin_markdown(
        self,
        actor: AuthContext,
        filename: str,
        path: Path,
    ) -> PersonalSkillDraft:
        """管理员端解析单个 Markdown 文件返回草稿，不落库且不创建版本。"""
        await self._authorize_skill_manager(actor)
        return await asyncio.to_thread(parse_personal_markdown, filename, path)

    async def create_admin_markdown(
        self,
        actor: AuthContext,
        *,
        name: str,
        description: str,
        guide: str,
        idempotency_key: str,
        scope: SkillScope = SkillScope.COMPANY,
        owner_department_id: str | None = None,
        visibility_grants: Sequence[VisibilityGrantInput] | None = None,
        slug: str | None = None,
        enabled: bool = True,
        featured: bool = False,
        starter_prompt: str | None = None,
    ) -> SkillAdminDetails:
        """在线创建公司级或部门级纯 Markdown 技能（复用三层分权、确定性打包与幂等重放）。"""
        (
            resolved_scope,
            resolved_department_id,
            resolved_grants,
        ) = await self._resolve_admin_create_visibility(
            actor,
            scope=scope,
            owner_department_id=owner_department_id,
            visibility_grants=visibility_grants,
        )
        idempotency_key = validate_idempotency_key(idempotency_key)
        draft = validate_personal_content(name, description, guide)
        normalized_slug = validate_optional_admin_slug(slug)
        normalized_starter_prompt = validate_starter_prompt(starter_prompt)

        # 幂等指纹显式包含 mode="admin_markdown" 及全部业务字段，避免与 ZIP 导入共享键时静默混淆；
        # slug 字段使用规范化后的显式输入（未传为 None），保证自动派生 slug 的网络重试可精确重放。
        fingerprint = _admin_markdown_create_fingerprint(
            actor=actor,
            draft=draft,
            scope=resolved_scope.value,
            owner_department_id=resolved_department_id,
            visibility_grants=resolved_grants,
            slug=normalized_slug,
            enabled=enabled,
            featured=featured,
            starter_prompt=normalized_starter_prompt,
        )
        replay = await self._find_admin_replay(
            actor,
            operation="import",
            target_skill_id=None,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return await self._admin_markdown_replay_details(actor, replay)

        skill_id = self._id_factory("skill")
        version_id = self._id_factory("skillver")
        effective_slug = normalized_slug or build_admin_markdown_slug(skill_id)
        manifest, package_sha256 = build_markdown_manifest(effective_slug, draft)
        # 管理端在线 Markdown 创建走纯数据库单事务，零 ZIP 构造、零 MinIO 依赖。
        return await commit_markdown_write(
            personal=False,
            write=lambda: self._repository.create_skill(
                skill_id=skill_id,
                version_id=version_id,
                label="v1",
                manifest=manifest,
                object_key=None,
                package_sha256=package_sha256,
                dependency_check=markdown_dependency_check(self._clock()),
                actor=actor,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                created_at=self._clock(),
                scope=resolved_scope,
                owner_department_id=resolved_department_id,
                visibility_grants=resolved_grants,
                format=SkillFormat.MARKDOWN,
                enabled=enabled,
                featured=featured,
                starter_prompt=normalized_starter_prompt,
            ),
            find_replay=lambda: self._find_admin_replay(
                actor,
                operation="import",
                target_skill_id=None,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
            ),
            on_permission_error=lambda _error: SkillAuthorizationError("admin_required"),
            auth_error_types=(SkillAuthorizationError,),
            replay_to_details=lambda result: self._admin_markdown_replay_details(actor, result),
            to_details=lambda pair: SkillAdminDetails(pair[0], (pair[1],)),
            map_slug_exists_to_conflict=True,
        )

    async def publish_admin_markdown_version(
        self,
        actor: AuthContext,
        skill_id: str,
        *,
        name: str,
        description: str,
        guide: str,
        expected_revision: int,
        idempotency_key: str,
        label: str | None = None,
        activate: bool = True,
    ) -> SkillAdminDetails:
        """为公司/部门在线 Markdown 技能发布新版本；严禁覆盖 ZIP 包技能，支持 CAS 与幂等。"""
        idempotency_key = validate_idempotency_key(idempotency_key)
        if expected_revision < 1:
            raise SkillPackageError("skill_revision_invalid")
        skill = await self._require_manageable_admin_skill(actor, skill_id)
        # 格式防覆盖底线：ZIP 技能包可能包含脚本与多文件资源，绝不允许在线单文件覆盖。
        if skill.format is not SkillFormat.MARKDOWN:
            raise SkillConflictError("skill_markdown_edit_unsupported")

        draft = validate_personal_content(name, description, guide)
        normalized_label = validate_version_label(label)
        fingerprint = _admin_markdown_version_fingerprint(
            actor=actor,
            target_skill_id=skill_id,
            expected_revision=expected_revision,
            draft=draft,
            label=normalized_label,
            activate=activate,
            scope=skill.scope.value,
            owner_department_id=skill.owner_department_id,
        )
        # 幂等重放先于 revision 预检，确保已成功提交后的网络重试不会因 revision 递增而误报 409。
        replay = await self._find_admin_replay(
            actor,
            operation="add_version",
            target_skill_id=skill_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return await self._admin_markdown_replay_details(actor, replay)

        if skill.revision != expected_revision:
            raise SkillConflictError("skill_revision_conflict")

        version_id = self._id_factory("skillver")
        manifest, package_sha256 = build_markdown_manifest(skill.slug, draft)
        return await commit_markdown_write(
            personal=False,
            write=lambda: self._repository.publish_admin_markdown_version(
                skill_id,
                version_id=version_id,
                expected_revision=expected_revision,
                label=normalized_label,
                activate=activate,
                manifest=manifest,
                object_key=None,
                package_sha256=package_sha256,
                dependency_check=markdown_dependency_check(self._clock()),
                actor=actor,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                created_at=self._clock(),
            ),
            find_replay=lambda: self._find_admin_replay(
                actor,
                operation="add_version",
                target_skill_id=skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
            ),
            on_permission_error=lambda _error: SkillAuthorizationError("admin_required"),
            auth_error_types=(SkillAuthorizationError,),
            replay_to_details=lambda result: self._admin_markdown_replay_details(actor, result),
            to_details_async=lambda result: self._admin_markdown_replay_details(actor, result),
        )

    async def import_package(
        self,
        actor: AuthContext,
        path: Path,
        *,
        idempotency_key: str,
        scope: SkillScope = SkillScope.COMPANY,
        owner_department_id: str | None = None,
        visibility_grants: Sequence[VisibilityGrantInput] | None = None,
    ) -> SkillAdminDetails:
        """首发导入完整 ZIP；支持 company 与 department 级三层分权校验。"""
        (
            resolved_scope,
            resolved_department_id,
            resolved_grants,
        ) = await self._resolve_admin_create_visibility(
            actor,
            scope=scope,
            owner_department_id=owner_department_id,
            visibility_grants=visibility_grants,
        )
        # ZIP 技能包必须依赖对象存储；未配置对象存储时提前快速拒绝为 503。
        self._require_object_store()

        idempotency_key = validate_idempotency_key(idempotency_key)
        validated = await self._validate_package_offloop(path)
        fingerprint = _import_fingerprint(
            actor=actor,
            operation="import",
            target_skill_id=None,
            package_sha256=validated.package_sha256,
            label=None,
            scope=resolved_scope.value,
            owner_department_id=resolved_department_id,
            visibility_grants=resolved_grants,
        )
        replay = await self._find_admin_replay(
            actor,
            operation="import",
            target_skill_id=None,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return single_version_details(replay)
        dependency_check = await check_dependencies(
            self._dependency_checker,
            validated.manifest.dependencies,
            clock=self._clock,
        )
        skill_id = self._id_factory("skill")
        version_id = self._id_factory("skillver")

        async def _write(object_key: str) -> SkillAdminDetails:
            skill, version = await self._repository.create_skill(
                skill_id=skill_id,
                version_id=version_id,
                label="v1",
                manifest=validated.manifest,
                object_key=object_key,
                package_sha256=validated.package_sha256,
                dependency_check=dependency_check,
                actor=actor,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                created_at=self._clock(),
                scope=resolved_scope,
                owner_department_id=resolved_department_id,
                visibility_grants=resolved_grants,
            )
            return SkillAdminDetails(skill, (version,))

        return await self._publish_zip_object(
            actor,
            path,
            operation="import",
            target_skill_id=None,
            version_id=version_id,
            package_sha256=validated.package_sha256,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            write=_write,
            extract_version_id=lambda details: details.versions[0].id,
            replay_to_result=single_version_details,
        )

    async def _find_admin_replay(
        self,
        actor: AuthContext,
        *,
        operation: str,
        target_skill_id: str | None,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> SkillImportRequestResult | None:
        """读取已提交管理端幂等请求，并将事务内撤权统一映射为 403。"""
        try:
            return await self._repository.find_import_result(
                actor,
                operation=operation,
                target_skill_id=target_skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
        except PermissionError as error:
            raise SkillAuthorizationError("admin_required") from error

    async def _replay_admin_after_failure(
        self,
        actor: AuthContext,
        *,
        operation: str,
        target_skill_id: str | None,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> SkillImportRequestResult | None:
        """ZIP 写入异常后回查已提交幂等结果；优先透传取消与撤权，未命中返回 None 以便回收对象。"""
        try:
            return await self._find_admin_replay(
                actor,
                operation=operation,
                target_skill_id=target_skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
        except asyncio.CancelledError:
            raise
        except SkillAuthorizationError:
            raise
        except Exception:
            return None

    async def _validate_visibility(
        self,
        *,
        user_id: str,
        visibility_scope: VisibilityScope,
        owner_department_id: str | None,
        visibility_grants: Sequence[VisibilityGrantInput] | None,
        for_create: bool,
        existing_target: ResourceAccessTarget | None = None,
        preserve_existing_grants_when_none: bool = True,
    ) -> tuple[VisibilityScope, str | None, tuple[VisibilityGrantInput, ...] | None]:
        """统一调用权限引擎校验可见性范围与授权，并将领域异常映射为 Skill 层业务异常。"""
        try:
            return await self._access_policy.resolve_and_validate_visibility(
                user_id=user_id,
                resource_type=ResourceType.SKILL,
                visibility_scope=visibility_scope,
                owner_department_id=owner_department_id,
                visibility_grants=visibility_grants,
                for_create=for_create,
                existing_target=existing_target,
                preserve_existing_grants_when_none=preserve_existing_grants_when_none,
            )
        except AccessPermissionDeniedError as error:
            raise SkillAuthorizationError(error.code) from error
        except InvalidVisibilityConfigError as error:
            raise SkillPackageError(error.code) from error

    async def _resolve_admin_create_visibility(
        self,
        actor: AuthContext,
        *,
        scope: SkillScope,
        owner_department_id: str | None,
        visibility_grants: Sequence[VisibilityGrantInput] | None,
    ) -> tuple[SkillScope, str | None, tuple[VisibilityGrantInput, ...]]:
        """校验管理端创建公司/部门技能的三层分权与可见性配置（ZIP 与在线 Markdown 共用）。"""
        user = await self._authorize_skill_manager(actor)
        if scope is SkillScope.PERSONAL:
            raise SkillPackageError("skill_scope_invalid")

        vis_scope, resolved_department_id, maybe_grants = await self._validate_visibility(
            user_id=user.id,
            visibility_scope=VisibilityScope(scope.value),
            owner_department_id=owner_department_id,
            visibility_grants=visibility_grants,
            for_create=True,
        )
        return SkillScope(vis_scope.value), resolved_department_id, (maybe_grants or ())

    async def _admin_markdown_replay_details(
        self,
        actor: AuthContext,
        replay: SkillImportRequestResult,
    ) -> SkillAdminDetails:
        """管理端 Markdown 幂等重放或发版后返回完整历史详情；若已被软删则稳定返回 404。"""
        if replay.skill.deleted_at is not None:
            raise SkillNotFoundError()
        return await self.get_admin(actor, replay.skill.id)

    async def _publish_zip_object[T](
        self,
        actor: AuthContext,
        path: Path,
        *,
        operation: str,
        target_skill_id: str | None,
        version_id: str,
        package_sha256: str,
        idempotency_key: str,
        request_fingerprint: str,
        write: Callable[[str], Awaitable[T | None]],
        extract_version_id: Callable[[T], str],
        replay_to_result: Callable[[SkillImportRequestResult], T],
    ) -> T:
        """先写入 ZIP 对象再提交数据库，失败或并发落败时严格按已知/未知态处置孤儿对象。"""
        object_store = self._require_object_store()
        object_key = self._package_key(package_sha256, version_id)
        try:
            await object_store.put_file(
                object_key,
                path,
                content_type="application/zip",
            )
        except BaseException:
            await self._discard_prepared_package(object_key)
            raise
        try:
            written = await write(object_key)
        except asyncio.CancelledError:
            # 取消可能发生在提交后的响应阶段，保留对象供幂等重放或安全离线回收。
            raise
        except PermissionError as error:
            # 事务内管理员复核失败统一映射为 403；提交态不明时不主动删除对象。
            raise SkillAuthorizationError("admin_required") from error
        except SkillConflictError:
            replay = await self._replay_admin_after_failure(
                actor,
                operation=operation,
                target_skill_id=target_skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                if (replay.request_version or replay.version).object_key != object_key:
                    await self._discard_prepared_package(object_key)
                return replay_to_result(replay)
            # 适配器只会把已确定性回滚的唯一约束冲突收敛为该业务异常，安全删除预写对象。
            await self._discard_prepared_package(object_key)
            raise
        except Exception:
            replay = await self._replay_admin_after_failure(
                actor,
                operation=operation,
                target_skill_id=target_skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                return replay_to_result(replay)
            # 临时未命中不能证明仍在提交的事务不会稍后提交，必须保留对象。
            raise
        if written is None:
            await self._discard_prepared_package(object_key)
            raise SkillNotFoundError()
        if extract_version_id(written) != version_id:
            # 并发写由唯一约束选出胜者，败者清理自己预写的未引用对象。
            await self._discard_prepared_package(object_key)
        return written

    async def add_version(
        self,
        actor: AuthContext,
        skill_id: str,
        path: Path,
        *,
        idempotency_key: str,
        label: str | None = None,
    ) -> SkillVersion:
        """上传完整更新包；slug 不符或越权时不写数据库、不改变当前指针。"""
        idempotency_key = validate_idempotency_key(idempotency_key)
        try:
            skill = await self._require_manageable_admin_skill(actor, skill_id)
        except PermissionError as error:
            if isinstance(error, SkillAuthorizationError):
                raise
            # add_version 的初始目标读取也可能在事务内发现管理员撤权。
            raise SkillAuthorizationError("admin_required") from error
        # 反向格式锁定：在线 Markdown 技能禁止上传 ZIP 技能包，在写对象存储前即快速拒绝。
        if skill.format is not SkillFormat.PACKAGE:
            raise SkillConflictError("skill_package_upload_unsupported")
        self._require_object_store()
        validated = await self._validate_package_offloop(path)
        if validated.manifest.slug != skill.slug:
            raise SkillConflictError("skill_slug_mismatch")
        fingerprint = _import_fingerprint(
            actor=actor,
            operation="add_version",
            target_skill_id=skill_id,
            package_sha256=validated.package_sha256,
            label=label,
            scope=skill.scope.value,
            owner_department_id=skill.owner_department_id,
        )
        replay = await self._find_admin_replay(
            actor,
            operation="add_version",
            target_skill_id=skill_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            # ZIP add_version 重放返回该幂等请求实际上传创建的版本（request_version）。
            return replay.request_version or replay.version
        dependency_check = await check_dependencies(
            self._dependency_checker,
            validated.manifest.dependencies,
            clock=self._clock,
        )
        version_id = self._id_factory("skillver")
        return await self._publish_zip_object(
            actor,
            path,
            operation="add_version",
            target_skill_id=skill_id,
            version_id=version_id,
            package_sha256=validated.package_sha256,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            write=lambda object_key: self._repository.add_version(
                skill_id,
                version_id=version_id,
                label=label,
                manifest=validated.manifest,
                object_key=object_key,
                package_sha256=validated.package_sha256,
                dependency_check=dependency_check,
                actor=actor,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                created_at=self._clock(),
            ),
            extract_version_id=lambda version: version.id,
            replay_to_result=lambda replay: replay.request_version or replay.version,
        )

    async def configure(
        self,
        actor: AuthContext,
        skill_id: str,
        *,
        expected_revision: int,
        enabled: bool,
        current_version_id: str,
        script_requires_approval: bool,
        visibility_grants: Sequence[VisibilityGrantInput] | None = None,
        featured: bool | UnsetType = UNSET,
        starter_prompt: str | None | UnsetType = UNSET,
    ) -> Skill:
        """按目标环境最新检查结果 CAS 切换版本、启停、审批、可见性授权及首页推荐元数据。"""
        skill = await self._require_manageable_admin_skill(actor, skill_id)
        normalized_starter_prompt: str | None | UnsetType = (
            UNSET if starter_prompt is UNSET else validate_starter_prompt(starter_prompt)
        )
        resolved_grants: tuple[VisibilityGrantInput, ...] | None = None
        if visibility_grants is not None:
            _, _, resolved_grants = await self._validate_visibility(
                user_id=actor.user.id,
                visibility_scope=VisibilityScope(skill.scope.value),
                owner_department_id=skill.owner_department_id,
                visibility_grants=visibility_grants,
                for_create=False,
                existing_target=_skill_access_target(skill),
                preserve_existing_grants_when_none=False,
            )

        version = await self._repository.get_version(current_version_id)
        if version is None or version.skill_id != skill_id:
            raise SkillNotFoundError("skill_version_not_found")
        if enabled:
            # 纯 Markdown 无依赖无脚本技能免于触发 Python 解释器子进程探测。
            if is_plain_markdown_skill(skill, version):
                dependency_check = markdown_dependency_check(self._clock())
            else:
                dependency_check = await check_dependencies(
                    self._dependency_checker,
                    version.dependency_requirements,
                    clock=self._clock,
                )
                if dependency_check.status is not DependencyCheckStatus.SATISFIED:
                    raise SkillDependencyUnsatisfiedError()
        else:
            # 停用必须始终可执行：保留原检查结论，不伪造一次“新鲜”探测，
            # 即使目标解释器已损坏也允许管理员先关闭 Skill。
            dependency_check = version.dependency_check
        try:
            result = await self._repository.update_configuration(
                skill_id,
                expected_revision=expected_revision,
                enabled=enabled,
                current_version_id=current_version_id,
                script_requires_approval=script_requires_approval,
                dependency_check=dependency_check,
                actor=actor,
                updated_at=self._clock(),
                visibility_grants=resolved_grants,
                featured=featured,
                starter_prompt=normalized_starter_prompt,
            )
        except PermissionError as error:
            # 事务内会再次锁定当前登录态；管理员失效统一收敛为 403 业务异常。
            raise SkillAuthorizationError("admin_required") from error
        if result is None:
            raise SkillNotFoundError()
        return result

    async def delete_admin(
        self,
        actor: AuthContext,
        skill_id: str,
        *,
        expected_revision: int,
    ) -> None:
        """软删除部门级 Skill（company 级只允许停用，不可删除；越权按 403/404 隔离）。"""
        if expected_revision < 1:
            raise SkillPackageError("skill_revision_invalid")
        skill = await self._require_manageable_admin_skill(actor, skill_id)
        if skill.scope is SkillScope.COMPANY:
            raise SkillPackageError("company_skill_delete_unsupported")
        try:
            await self._repository.delete_admin_skill(
                skill_id,
                expected_revision=expected_revision,
                actor=actor,
                deleted_at=self._clock(),
            )
        except PermissionError as error:
            raise SkillAuthorizationError("admin_required") from error
        except ValueError as error:
            raise SkillPackageError(str(error)) from error

    # ------------------------------------------------------------------
    # 用户目录与 Run 执行期只读组合门面（委托 SkillRuntimeReader）
    # ------------------------------------------------------------------

    async def user_directory(self, actor: AuthContext) -> tuple[Skill, ...]:
        """返回对当前用户实时可读的 company、department 及本人 personal 启用 Skill。"""
        return await self._runtime_reader.user_directory(actor)

    async def build_run_snapshot(
        self,
        user_id: str,
        selected_skill_ids: Sequence[str] = (),
    ) -> RunSkillSnapshot:
        """基于当前用户实时三层权限构建 Run Skill 快照，拒绝无权访问的选中项。"""
        return await self._runtime_reader.build_run_snapshot(user_id, selected_skill_ids)

    async def resolve_run_entry(
        self,
        snapshot: RunSkillSnapshot,
        skill_id: str,
    ) -> tuple[RunSkillEntry, SkillVersion]:
        """只通过 Run 固定版本身份解析技能，禁止回读当前目录。"""
        return await self._runtime_reader.resolve_run_entry(snapshot, skill_id)

    async def prompt_context(
        self,
        snapshot: RunSkillSnapshot,
        active_skill_ids: tuple[str, ...],
    ) -> RunSkillPromptContext:
        """按 Run 固定目录顺序生成目录和可重建的活跃完整指南。"""
        return await self._runtime_reader.prompt_context(snapshot, active_skill_ids)

    async def prepare_script_target(
        self,
        snapshot: RunSkillSnapshot,
        *,
        skill_id: str,
        script_path: str,
        parameters: dict[str, object],
        use_cached_dependency_check: bool = False,
    ) -> tuple[
        RunSkillEntry,
        SkillVersion,
        SkillScriptDefinition,
        DependencyCheck,
    ]:
        """解析固定版本，并返回已刷新或缓存复用的当前 Worker 环境依赖结论。"""
        return await self._runtime_reader.prepare_script_target(
            snapshot,
            skill_id=skill_id,
            script_path=script_path,
            parameters=parameters,
            use_cached_dependency_check=use_cached_dependency_check,
        )

    async def active_entries(
        self,
        snapshot: RunSkillSnapshot,
        active_skill_ids: tuple[str, ...],
    ) -> tuple[tuple[RunSkillEntry, SkillVersion], ...]:
        """按 Run 目录顺序解析选中和成功加载的技能正文。"""
        return await self._runtime_reader.active_entries(snapshot, active_skill_ids)

    async def recheck_version_dependencies(
        self,
        version: SkillVersion,
        *,
        use_cached_dependency_check: bool = False,
    ) -> tuple[SkillVersion, DependencyCheck]:
        """执行前复核依赖并仅在实质结果变化时写回数据库，消除 checked_at 导致的写放大。"""
        return await self._runtime_reader.recheck_version_dependencies(
            version,
            use_cached_dependency_check=use_cached_dependency_check,
        )

    async def read_resource(
        self,
        snapshot: RunSkillSnapshot,
        *,
        skill_id: str,
        path: str,
        offset: int,
        limit_bytes: int,
    ) -> SkillResourceSlice:
        """从 Run 固定版本读取文本资源并保持 O(1) UTF-8 字符边界校验。"""
        return await self._runtime_reader.read_resource(
            snapshot,
            skill_id=skill_id,
            path=path,
            offset=offset,
            limit_bytes=limit_bytes,
        )

    # ------------------------------------------------------------------
    # 执行审计与管理员鉴权辅助
    # ------------------------------------------------------------------

    async def _validate_package_offloop(self, path: Path) -> ValidatedSkillPackage:
        """在工作线程校验上传 ZIP；取消时盾护等待线程退出，防止 staged_upload 抢先删文件。"""
        return await run_thread_shielded(
            self._package_validator.validate,
            path,
            name="athena-skill-validate-package",
        )

    async def admin_execution(self, actor: AuthContext, call_id: str) -> SkillExecutionRecord:
        """读取管理员可追溯的脚本环境、错误与日志引用。"""
        await self._authorize_admin(actor)
        execution = await self._repository.get_admin_execution(call_id)
        if execution is None:
            raise SkillNotFoundError("skill_execution_not_found")
        return execution

    async def admin_log(self, actor: AuthContext, call_id: str) -> bytes:
        """管理员按受控标识读取最多 5 MiB 的原始执行日志。"""
        execution = await self.admin_execution(actor, call_id)
        if execution.log_object_key is None:
            raise SkillNotFoundError("skill_execution_log_not_found")
        object_store = self._require_object_store()
        return await object_store.get_bytes(
            execution.log_object_key,
            maximum_bytes=5 * 1024 * 1024,
        )

    async def _authorize_roles(
        self,
        actor: AuthContext,
        allowed_roles: frozenset[UserRole],
    ) -> User:
        """校验当前登录会话仍有效且具备允许的管理角色。"""
        return await require_session_roles(
            self._auth_reader,
            actor,
            allowed_roles,
            self._clock(),
            invalid_session_error=lambda: SkillAuthorizationError("admin_required"),
            forbidden_error=lambda: SkillAuthorizationError("admin_required"),
        )

    async def _authorize_admin(self, actor: AuthContext) -> None:
        await self._authorize_roles(actor, frozenset({UserRole.SUPER_ADMIN}))

    async def _authorize_skill_manager(self, actor: AuthContext) -> User:
        """校验当前登录态为活跃超级管理员或部门管理员。"""
        return await self._authorize_roles(
            actor,
            frozenset({UserRole.SUPER_ADMIN, UserRole.DEPT_ADMIN}),
        )

    async def _require_manageable_admin_skill(self, actor: AuthContext, skill_id: str) -> Skill:
        """校验管理端对目标 Skill 的访问权：不可读返回 404，可读但不可管理返回 403。"""
        user = await self._authorize_skill_manager(actor)
        skill = await self._repository.get_skill(skill_id)
        if skill is None:
            raise SkillNotFoundError()
        if user.role is UserRole.SUPER_ADMIN:
            return skill
        target = _skill_access_target(skill)
        # 单次快照合并可读与可管双判定，避免重复加载用户组织树。
        can_read, can_manage = await self._access_policy.evaluate_target_access(user.id, target)
        if can_manage:
            return skill
        # 对不可读的他部门技能返回 404 防止探测存在性；对可读但无管理权（如 company 级）返回 403。
        if can_read:
            raise SkillAuthorizationError("admin_required")
        raise SkillNotFoundError()

    async def _discard_prepared_package(self, object_key: str) -> None:
        """删除已确认未发布的包对象。"""
        if self._object_store is None:
            return
        try:
            await self._object_store.delete(object_key)
        except Exception:
            pass

    @staticmethod
    def _package_key(sha256: str, version_id: str) -> str:
        return f"skills/{sha256[:2]}/{sha256}/{version_id}.zip"


def _normalize_grants_for_fingerprint(
    visibility_grants: Sequence[VisibilityGrantInput],
) -> list[tuple[str, str, bool]]:
    """将可见性授权按确定性顺序展开，消除调用顺序差异对幂等指纹的影响。"""
    return sorted(
        (grant.target_type.value, grant.target_id, grant.include_subdepartments)
        for grant in visibility_grants
    )


def _import_fingerprint(
    *,
    actor: AuthContext,
    operation: str,
    target_skill_id: str | None,
    package_sha256: str,
    label: str | None,
    scope: str = SkillScope.COMPANY.value,
    owner_department_id: str | None = None,
    visibility_grants: Sequence[VisibilityGrantInput] = (),
) -> str:
    """摘要覆盖管理员、操作、目标、可见范围、归属部门、规范化授权、包正文和展示标签。"""
    return sha256_json(
        {
            "operation": operation,
            "owner_id": actor.user.id,
            "target_skill_id": target_skill_id,
            "package_sha256": package_sha256,
            "label": label,
            "scope": scope,
            "owner_department_id": owner_department_id,
            "visibility_grants": _normalize_grants_for_fingerprint(visibility_grants),
        }
    )


def _admin_markdown_create_fingerprint(
    *,
    actor: AuthContext,
    draft: PersonalSkillDraft,
    scope: str,
    owner_department_id: str | None,
    visibility_grants: Sequence[VisibilityGrantInput],
    slug: str | None,
    enabled: bool,
    featured: bool,
    starter_prompt: str | None,
) -> str:
    """管理端在线 Markdown 创建幂等指纹，覆盖全部可写业务字段并与 ZIP 导入显式隔离。"""
    return sha256_json(
        {
            "mode": "admin_markdown",
            "operation": "import",
            "owner_id": actor.user.id,
            "target_skill_id": None,
            "name": draft.name,
            "description": draft.description,
            "guide": draft.guide,
            "scope": scope,
            "owner_department_id": owner_department_id,
            "visibility_grants": _normalize_grants_for_fingerprint(visibility_grants),
            "slug": slug,
            "enabled": enabled,
            "featured": featured,
            "starter_prompt": starter_prompt,
        }
    )


def _admin_markdown_version_fingerprint(
    *,
    actor: AuthContext,
    target_skill_id: str,
    expected_revision: int,
    draft: PersonalSkillDraft,
    label: str | None,
    activate: bool,
    scope: str,
    owner_department_id: str | None,
) -> str:
    """管理端在线 Markdown 发布新版本幂等指纹，覆盖修订号、版本正文、标签与激活标志。"""
    return sha256_json(
        {
            "mode": "admin_markdown_version",
            "operation": "add_version",
            "owner_id": actor.user.id,
            "target_skill_id": target_skill_id,
            "expected_revision": expected_revision,
            "name": draft.name,
            "description": draft.description,
            "guide": draft.guide,
            "label": label,
            "activate": activate,
            "scope": scope,
            "owner_department_id": owner_department_id,
        }
    )


__all__ = [
    "SkillAuthorizationError",
    "SkillPackageError",
    "SkillService",
    "SkillUserAuthorizationError",
]
