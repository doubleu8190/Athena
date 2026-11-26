"""Skill 包导入、公开目录、版本切换和资源读取用例。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import zipfile
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from io import BytesIO
from pathlib import Path

from app.access import AccessPolicyService
from app.skill.dependency import DependencyChecker
from app.skill.model import SkillAdminDetails, SkillResourceSlice
from app.skill.package import (
    SkillPackageValidator,
    ValidatedSkillPackage,
    validate_parameter_schema,
)
from app.skill.package_cache import SkillPackageCache, safe_relative_posix_parts
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
from common.async_utils import run_thread_shielded
from common.ids import new_id, now_utc
from domain.access import (
    AccessPermissionDeniedError,
    InvalidVisibilityConfigError,
    ResourceAccessTarget,
    ResourceType,
    VisibilityGrantInput,
    VisibilityScope,
)
from domain.auth import AuthContext, CurrentSessionReader, User, UserRole
from domain.file import FileObjectStore, FileStorageError
from domain.skill import (
    MAX_PACKAGE_ZIP_BYTES,
    UNSET,
    ActiveSkillGuide,
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
    SkillPackageManifest,
    SkillRepository,
    SkillScope,
    SkillScriptDefinition,
    SkillVersion,
    SkillVersionPage,
    UnsetType,
)


class SkillAuthorizationError(PermissionError):
    """管理操作未通过事务外的当前管理员预检。"""


class SkillUserAuthorizationError(PermissionError):
    """个人 Skill 写事务发现当前登录账号或会话失效。"""


_IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{1,128}$")
# 用户可用目录排序优先级：推荐置顶优先，其后按部门 > 公司 > 个人分层排序。
_DIRECTORY_SCOPE_PRIORITY: dict[SkillScope, int] = {
    SkillScope.DEPARTMENT: 0,
    SkillScope.COMPANY: 1,
    SkillScope.PERSONAL: 2,
}


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
    """管理不可变包版本，按三层权限引擎分权管控，并在目标环境复核依赖。"""

    def __init__(
        self,
        repository: SkillRepository,
        object_store: FileObjectStore | None,
        auth_reader: CurrentSessionReader,
        *,
        access_policy: AccessPolicyService | None = None,
        package_validator: SkillPackageValidator | None = None,
        dependency_checker: DependencyChecker | None = None,
        package_cache: SkillPackageCache | None = None,
        id_factory: Callable[[str], str] = new_id,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """保存持久化、可选对象存储（仅 ZIP/日志需要）、认证、权限引擎与依赖检查器。"""
        self._repository = repository
        self._object_store = object_store
        self._auth_reader = auth_reader
        self._access_policy = access_policy
        self._package_validator = package_validator or SkillPackageValidator()
        self._dependency_checker = dependency_checker or DependencyChecker(clock=clock)
        self._package_cache = package_cache
        self._id_factory = id_factory
        self._clock = clock

    def _require_object_store(self) -> FileObjectStore:
        """对象存储未启用时拒绝 ZIP 导入、ZIP 资源读取与脚本日志下载，统一映射 503。"""
        if self._object_store is None:
            raise FileStorageError("file_storage_unavailable")
        return self._object_store

    async def list_admin(self, actor: AuthContext) -> tuple[Skill, ...]:
        """读取当前管理员可维护目录项（超管全量，部门管理员仅管辖部门）。"""
        user = await self._authorize_skill_manager(actor)
        skills = await self._repository.list_skills()
        if user.role is UserRole.SUPER_ADMIN:
            return skills
        if self._access_policy is None:
            return ()
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

    async def list_personal(self, actor: AuthContext) -> tuple[Skill, ...]:
        """读取当前用户未删除的个人 Skill，包含停用项。"""
        return await self._repository.list_personal_skills(actor.user.id)

    async def get_personal(
        self,
        actor: AuthContext,
        skill_id: str,
    ) -> SkillAdminDetails:
        """读取当前用户个人 Skill 详情及当前版本正文。"""
        skill = await self._repository.get_personal_skill(actor.user.id, skill_id)
        if skill is None:
            raise SkillNotFoundError()
        return await self._personal_details(skill)

    async def parse_personal_document(
        self,
        filename: str,
        path: Path,
    ) -> PersonalSkillDraft:
        """在异步服务边界外执行有界 Markdown 文件解析。"""
        return await asyncio.to_thread(parse_personal_markdown, filename, path)

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
        draft = validate_personal_content(name, description, guide)
        idempotency_key = _validate_idempotency_key(idempotency_key)
        fingerprint = _personal_fingerprint(
            actor=actor,
            operation="personal_create",
            target_skill_id=None,
            expected_revision=None,
            draft=draft,
        )
        replay = await self._find_personal_replay(
            actor,
            operation="personal_create",
            target_skill_id=None,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return SkillAdminDetails(replay.skill, (replay.version,))

        skill_id = self._id_factory("skill")
        version_id = self._id_factory("skillver")
        manifest, package_sha256 = build_markdown_manifest(_personal_slug(skill_id), draft)
        # 纯 Markdown 个人技能走纯数据库单事务，零 ZIP 构造、零 MinIO 依赖。
        return await self._commit_markdown_write(
            actor,
            personal=True,
            operation="personal_create",
            target_skill_id=None,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            write=lambda: self._repository.create_personal_skill(
                skill_id=skill_id,
                version_id=version_id,
                manifest=manifest,
                object_key=None,
                package_sha256=package_sha256,
                dependency_check=self._personal_dependency_check(),
                actor=actor,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                created_at=self._clock(),
            ),
            to_details=lambda result: SkillAdminDetails(result.skill, (result.version,)),
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
        if expected_revision < 1:
            raise ValueError("skill_revision_invalid")
        draft = validate_personal_content(name, description, guide)
        idempotency_key = _validate_idempotency_key(idempotency_key)
        fingerprint = _personal_fingerprint(
            actor=actor,
            operation="personal_publish",
            target_skill_id=skill_id,
            expected_revision=expected_revision,
            draft=draft,
        )
        replay = await self._find_personal_replay(
            actor,
            operation="personal_publish",
            target_skill_id=skill_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if replay is not None:
            return SkillAdminDetails(replay.skill, (replay.version,))
        skill = await self._repository.get_personal_skill(actor.user.id, skill_id)
        if skill is None:
            raise SkillNotFoundError()
        if skill.revision != expected_revision:
            raise SkillConflictError("skill_revision_conflict")

        version_id = self._id_factory("skillver")
        manifest, package_sha256 = build_markdown_manifest(skill.slug, draft)
        return await self._commit_markdown_write(
            actor,
            personal=True,
            operation="personal_publish",
            target_skill_id=skill_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            write=lambda: self._repository.publish_personal_version(
                skill_id,
                version_id=version_id,
                expected_revision=expected_revision,
                manifest=manifest,
                object_key=None,
                package_sha256=package_sha256,
                dependency_check=self._personal_dependency_check(),
                actor=actor,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                created_at=self._clock(),
            ),
            to_details=lambda result: SkillAdminDetails(result.skill, (result.version,)),
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
        if expected_revision < 1:
            raise ValueError("skill_revision_invalid")
        try:
            skill = await self._repository.set_personal_enabled(
                skill_id,
                expected_revision=expected_revision,
                enabled=enabled,
                actor=actor,
                updated_at=self._clock(),
            )
        except PermissionError as error:
            raise SkillUserAuthorizationError("not_authenticated") from error
        if skill is None:
            raise SkillNotFoundError()
        return await self._personal_details(skill)

    async def delete_personal(
        self,
        actor: AuthContext,
        skill_id: str,
        *,
        expected_revision: int,
    ) -> None:
        """软删除当前用户自己的个人 Skill，历史版本保持不变。"""
        if expected_revision < 1:
            raise ValueError("skill_revision_invalid")
        try:
            await self._repository.delete_personal_skill(
                skill_id,
                expected_revision=expected_revision,
                actor=actor,
                deleted_at=self._clock(),
            )
        except PermissionError as error:
            raise SkillUserAuthorizationError("not_authenticated") from error

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
        idempotency_key = _validate_idempotency_key(idempotency_key)
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
        try:
            replay = await self._repository.find_import_result(
                actor,
                operation="import",
                target_skill_id=None,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
            )
        except PermissionError as error:
            raise SkillAuthorizationError("admin_required") from error
        if replay is not None:
            if replay.skill.deleted_at is not None:
                raise SkillNotFoundError()
            return await self._admin_details_for_replay(actor, replay)

        skill_id = self._id_factory("skill")
        version_id = self._id_factory("skillver")
        effective_slug = normalized_slug or build_admin_markdown_slug(skill_id)
        manifest, package_sha256 = build_markdown_manifest(effective_slug, draft)
        # 管理端在线 Markdown 创建走纯数据库单事务，零 ZIP 构造、零 MinIO 依赖。
        return await self._commit_markdown_write(
            actor,
            personal=False,
            operation="import",
            target_skill_id=None,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            write=lambda: self._repository.create_skill(
                skill_id=skill_id,
                version_id=version_id,
                label="v1",
                manifest=manifest,
                object_key=None,
                package_sha256=package_sha256,
                dependency_check=self._personal_dependency_check(),
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
        idempotency_key = _validate_idempotency_key(idempotency_key)
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
        try:
            replay = await self._repository.find_import_result(
                actor,
                operation="add_version",
                target_skill_id=skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
            )
        except PermissionError as error:
            raise SkillAuthorizationError("admin_required") from error
        if replay is not None:
            if replay.skill.deleted_at is not None:
                raise SkillNotFoundError()
            return await self._admin_details_for_replay(actor, replay)

        if skill.revision != expected_revision:
            raise SkillConflictError("skill_revision_conflict")

        version_id = self._id_factory("skillver")
        manifest, package_sha256 = build_markdown_manifest(skill.slug, draft)
        return await self._commit_markdown_write(
            actor,
            personal=False,
            operation="add_version",
            target_skill_id=skill_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            write=lambda: self._repository.publish_admin_markdown_version(
                skill_id,
                version_id=version_id,
                expected_revision=expected_revision,
                label=normalized_label,
                activate=activate,
                manifest=manifest,
                object_key=None,
                package_sha256=package_sha256,
                dependency_check=self._personal_dependency_check(),
                actor=actor,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                created_at=self._clock(),
            ),
            to_details_async=lambda result: self._admin_details_for_replay(actor, result),
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

        idempotency_key = _validate_idempotency_key(idempotency_key)
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
        try:
            replay = await self._repository.find_import_result(
                actor,
                operation="import",
                target_skill_id=None,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
            )
        except PermissionError as error:
            # 初始幂等读取同样在管理事务内复核登录态，撤权必须稳定返回 403。
            raise SkillAuthorizationError("admin_required") from error
        if replay is not None:
            return SkillAdminDetails(replay.skill, (replay.version,))
        dependency_check = await asyncio.to_thread(
            self._dependency_checker.check,
            validated.manifest.dependencies,
            checked_at=self._clock(),
        )
        return await self._publish_import(
            actor,
            path,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            manifest=validated.manifest,
            package_sha256=validated.package_sha256,
            dependency_check=dependency_check,
            scope=resolved_scope,
            owner_department_id=resolved_department_id,
            visibility_grants=resolved_grants,
        )

    async def _commit_markdown_write[T](
        self,
        actor: AuthContext,
        *,
        personal: bool,
        operation: str,
        target_skill_id: str | None,
        idempotency_key: str,
        request_fingerprint: str,
        write: Callable[[], Awaitable[T | None]],
        to_details: Callable[[T], SkillAdminDetails] | None = None,
        to_details_async: Callable[[T], Awaitable[SkillAdminDetails]] | None = None,
        map_slug_exists_to_conflict: bool = False,
    ) -> SkillAdminDetails:
        """统一执行 4 条纯 Markdown 写路径的单库事务与失败后幂等重放恢复。"""
        try:
            written = await write()
        except asyncio.CancelledError:
            raise
        except PermissionError as error:
            if personal:
                raise SkillUserAuthorizationError("not_authenticated") from error
            raise SkillAuthorizationError("admin_required") from error
        except Exception as error:
            replay = (
                await self._replay_personal_after_failure(
                    actor,
                    operation=operation,
                    target_skill_id=target_skill_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )
                if personal
                else await self._replay_after_import_failure(
                    actor,
                    operation=operation,
                    target_skill_id=target_skill_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )
            )
            if replay is not None:
                if personal:
                    return SkillAdminDetails(replay.skill, (replay.version,))
                if replay.skill.deleted_at is not None:
                    raise SkillNotFoundError() from None
                return await self._admin_details_for_replay(actor, replay)
            if (
                map_slug_exists_to_conflict
                and isinstance(error, SkillConflictError)
                and error.code == "skill_slug_exists"
            ):
                raise SkillConflictError("skill_slug_conflict") from error
            raise
        if written is None:
            raise SkillNotFoundError()
        if to_details_async is not None:
            return await to_details_async(written)
        assert to_details is not None
        return to_details(written)

    async def _find_personal_replay(
        self,
        actor: AuthContext,
        *,
        operation: str,
        target_skill_id: str | None,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> SkillImportRequestResult | None:
        """读取已提交个人请求，并把撤权映射为稳定的未认证错误。"""
        try:
            return await self._repository.find_personal_request_result(
                actor,
                operation=operation,
                target_skill_id=target_skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
        except PermissionError as error:
            raise SkillUserAuthorizationError("not_authenticated") from error

    async def _replay_personal_after_failure(
        self,
        actor: AuthContext,
        *,
        operation: str,
        target_skill_id: str | None,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> SkillImportRequestResult | None:
        """数据库异常后尝试读取已提交结果；普通查询失败保持未知提交。"""
        try:
            return await self._find_personal_replay(
                actor,
                operation=operation,
                target_skill_id=target_skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
        except asyncio.CancelledError:
            raise
        except SkillUserAuthorizationError:
            raise
        except (SkillConflictError, SkillNotFoundError):
            # 恢复查询已经给出明确业务结论时必须返回该结论；尤其删除后
            # 不能吞掉 404 再暴露原始数据库异常。
            raise
        except Exception:
            return None

    async def _personal_details(self, skill: Skill) -> SkillAdminDetails:
        """读取个人当前版本，并验证身份与版本没有失配。"""
        if skill.current_version_id is None:
            raise SkillConflictError("skill_catalog_integrity_invalid")
        version = await self._repository.get_version(skill.current_version_id)
        if version is None or version.skill_id != skill.id:
            raise SkillConflictError("skill_catalog_integrity_invalid")
        return SkillAdminDetails(skill, (version,))

    def _personal_dependency_check(self) -> DependencyCheck:
        """为无依赖个人正文写入明确的不探测兼容结论。"""
        return DependencyCheck(
            status=DependencyCheckStatus.SATISFIED,
            python_version="not-checked",
            python_implementation="markdown-only",
            items=(),
            checked_at=self._clock(),
        )

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

        if self._access_policy is not None:
            try:
                (
                    vis_scope,
                    resolved_department_id,
                    maybe_grants,
                ) = await self._access_policy.resolve_and_validate_visibility(
                    user_id=user.id,
                    resource_type=ResourceType.SKILL,
                    visibility_scope=VisibilityScope(scope.value),
                    owner_department_id=owner_department_id,
                    visibility_grants=visibility_grants,
                    for_create=True,
                )
            except AccessPermissionDeniedError as error:
                raise SkillAuthorizationError(error.code) from error
            except InvalidVisibilityConfigError as error:
                raise SkillPackageError(error.code) from error
            return SkillScope(vis_scope.value), resolved_department_id, (maybe_grants or ())

        if user.role is not UserRole.SUPER_ADMIN:
            raise SkillAuthorizationError("admin_required")
        if scope is not SkillScope.COMPANY or owner_department_id is not None:
            raise SkillPackageError("skill_scope_requires_access_policy")
        return scope, owner_department_id, ()

    async def _admin_details_for_replay(
        self,
        actor: AuthContext,
        replay: SkillImportRequestResult,
    ) -> SkillAdminDetails:
        """幂等重放或新版本发布后返回包含完整版本历史与当前有效指针的管理详情。"""
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
            replay = await self._replay_after_import_failure(
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
            replay = await self._replay_after_import_failure(
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

    async def _publish_import(
        self,
        actor: AuthContext,
        path: Path,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        manifest: SkillPackageManifest,
        package_sha256: str,
        dependency_check: DependencyCheck,
        scope: SkillScope = SkillScope.COMPANY,
        owner_department_id: str | None = None,
        visibility_grants: tuple[VisibilityGrantInput, ...] = (),
    ) -> SkillAdminDetails:
        """先写入首发包，再提交 Skill 身份、可见性授权和版本引用。"""
        skill_id = self._id_factory("skill")
        version_id = self._id_factory("skillver")

        async def _write(object_key: str) -> SkillAdminDetails:
            skill, version = await self._repository.create_skill(
                skill_id=skill_id,
                version_id=version_id,
                label="v1",
                manifest=manifest,
                object_key=object_key,
                package_sha256=package_sha256,
                dependency_check=dependency_check,
                actor=actor,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                created_at=self._clock(),
                scope=scope,
                owner_department_id=owner_department_id,
                visibility_grants=visibility_grants,
            )
            return SkillAdminDetails(skill, (version,))

        return await self._publish_zip_object(
            actor,
            path,
            operation="import",
            target_skill_id=None,
            version_id=version_id,
            package_sha256=package_sha256,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            write=_write,
            extract_version_id=lambda details: details.versions[0].id,
            replay_to_result=lambda replay: SkillAdminDetails(replay.skill, (replay.version,)),
        )

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
        idempotency_key = _validate_idempotency_key(idempotency_key)
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
        try:
            replay = await self._repository.find_import_result(
                actor,
                operation="add_version",
                target_skill_id=skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
            )
        except PermissionError as error:
            # 与导入一致：提交前查询也不得把撤权泄漏成 500。
            raise SkillAuthorizationError("admin_required") from error
        if replay is not None:
            # ZIP add_version 重放返回该幂等请求实际上传创建的版本（request_version）。
            return replay.request_version or replay.version
        dependency_check = await asyncio.to_thread(
            self._dependency_checker.check,
            validated.manifest.dependencies,
            checked_at=self._clock(),
        )
        return await self._publish_added_version(
            actor,
            skill_id,
            path,
            label=label,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            manifest=validated.manifest,
            package_sha256=validated.package_sha256,
            dependency_check=dependency_check,
        )

    async def _publish_added_version(
        self,
        actor: AuthContext,
        skill_id: str,
        path: Path,
        *,
        label: str | None,
        idempotency_key: str,
        request_fingerprint: str,
        manifest: SkillPackageManifest,
        package_sha256: str,
        dependency_check: DependencyCheck,
    ) -> SkillVersion:
        """先写入包对象，再追加版本并保留提交状态不明对象。"""
        version_id = self._id_factory("skillver")
        return await self._publish_zip_object(
            actor,
            path,
            operation="add_version",
            target_skill_id=skill_id,
            version_id=version_id,
            package_sha256=package_sha256,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            write=lambda object_key: self._repository.add_version(
                skill_id,
                version_id=version_id,
                label=label,
                manifest=manifest,
                object_key=object_key,
                package_sha256=package_sha256,
                dependency_check=dependency_check,
                actor=actor,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
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
            if self._access_policy is None:
                raise SkillPackageError("skill_scope_requires_access_policy")
            try:
                _, _, resolved_grants = await self._access_policy.resolve_and_validate_visibility(
                    user_id=actor.user.id,
                    resource_type=ResourceType.SKILL,
                    visibility_scope=VisibilityScope(skill.scope.value),
                    owner_department_id=skill.owner_department_id,
                    visibility_grants=visibility_grants,
                    for_create=False,
                    existing_target=_skill_access_target(skill),
                    preserve_existing_grants_when_none=False,
                )
            except AccessPermissionDeniedError as error:
                raise SkillAuthorizationError(error.code) from error
            except InvalidVisibilityConfigError as error:
                raise SkillPackageError(error.code) from error

        version = await self._repository.get_version(current_version_id)
        if version is None or version.skill_id != skill_id:
            raise SkillNotFoundError("skill_version_not_found")
        if enabled:
            # 纯 Markdown 无依赖无脚本技能免于触发 Python 解释器子进程探测。
            if (
                skill.format is SkillFormat.MARKDOWN
                and not version.dependency_requirements
                and not version.manifest.scripts
            ):
                dependency_check = self._personal_dependency_check()
            else:
                dependency_check = await asyncio.to_thread(
                    self._dependency_checker.check,
                    version.dependency_requirements,
                    checked_at=self._clock(),
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

    async def public_directory(self) -> tuple[Skill, ...]:
        """返回实时满足当前解释器依赖的已启用公司级 Skill。"""
        entries = await self._repository.get_public_directory_entries()
        return await self._available_directory_entries(entries)

    async def _load_readable_user_entries(
        self,
        user_id: str,
    ) -> tuple[tuple[Skill, SkillVersion], ...]:
        """加载用户实时可读的 (Skill, 当前 SkillVersion) 候选集。"""
        if self._access_policy is not None:
            return await self._repository.get_readable_user_directory_entries(user_id)
        return await self._repository.get_user_directory_entries(user_id)

    async def user_directory(self, actor: AuthContext) -> tuple[Skill, ...]:
        """返回对当前用户实时可读的 company、department 及本人 personal 启用 Skill。"""
        entries = await self._load_readable_user_entries(actor.user.id)
        return _sort_user_directory(await self._available_directory_entries(entries))

    async def build_run_snapshot(
        self,
        user_id: str,
        selected_skill_ids: Sequence[str] = (),
    ) -> RunSkillSnapshot:
        """基于当前用户实时三层权限构建 Run Skill 快照，拒绝无权访问的选中项。"""
        selected_tuple = tuple(selected_skill_ids)
        if len(selected_tuple) != len(set(selected_tuple)):
            raise SkillConflictError("skill_selection_invalid")
        entries = await self._load_readable_user_entries(user_id)
        available_ids = {skill.id for skill, _ in entries}
        if not set(selected_tuple) <= available_ids:
            raise SkillConflictError("skill_selection_unavailable")

        selected_set = set(selected_tuple)
        catalog = tuple(
            RunSkillEntry(
                skill_id=skill.id,
                slug=skill.slug,
                name=version.manifest.name,
                description=version.manifest.description,
                version_id=version.id,
                version_label=version.label,
                requires_approval=skill.script_requires_approval,
                scope=skill.scope,
            )
            for skill, version in entries
        )
        return RunSkillSnapshot(
            skills=catalog,
            selected_skill_ids=tuple(
                item.skill_id for item in catalog if item.skill_id in selected_set
            ),
        )

    async def _available_directory_entries(
        self,
        entries: tuple[tuple[Skill, SkillVersion], ...],
    ) -> tuple[Skill, ...]:
        """纯只读实时过滤公共目录依赖，个人与纯 Markdown 技能直接放行，GET 路径绝不写库。"""
        available: list[Skill] = []
        for skill, version in entries:
            if skill.current_version_id is None or version.skill_id != skill.id:
                continue
            if skill.scope is SkillScope.PERSONAL:
                if (
                    version.dependency_requirements
                    or version.manifest.scripts
                    or skill.script_requires_approval
                ):
                    raise SkillConflictError("skill_catalog_integrity_invalid")
                available.append(skill)
                continue
            # 纯 Markdown 无依赖无脚本技能直接放行，避免目录查询触发 Python 环境探测。
            if (
                skill.format is SkillFormat.MARKDOWN
                and not version.dependency_requirements
                and not version.manifest.scripts
            ):
                available.append(skill)
                continue
            # GET 目录仅做只读实时过滤，严禁在此触发 record_dependency_check 写库；
            # 依赖检查持久化仅发生在导入、新增版本、配置修改与执行前显式复核。
            check = await asyncio.to_thread(
                self._dependency_checker.check_cached,
                version.dependency_requirements,
            )
            if check.status is DependencyCheckStatus.SATISFIED:
                available.append(skill)
        return tuple(available)

    async def resolve_run_entry(
        self,
        snapshot: RunSkillSnapshot,
        skill_id: str,
    ) -> tuple[RunSkillEntry, SkillVersion]:
        """只通过 Run 固定版本身份解析技能，禁止回读当前目录。"""
        entry = snapshot.by_id(skill_id)
        if entry is None:
            raise SkillNotFoundError("skill_not_in_run_snapshot")
        version = await self._repository.get_version(entry.version_id)
        if version is None or version.skill_id != skill_id:
            raise SkillNotFoundError("skill_version_unavailable")
        return entry, version

    async def prompt_context(
        self,
        snapshot: RunSkillSnapshot,
        active_skill_ids: tuple[str, ...],
    ) -> RunSkillPromptContext:
        """按 Run 固定目录顺序生成目录和可重建的活跃完整指南。"""
        active_ids = set(active_skill_ids)
        unknown = active_ids - {item.skill_id for item in snapshot.skills}
        if unknown:
            raise SkillNotFoundError("skill_loaded_identity_invalid")
        active = tuple(
            ActiveSkillGuide(
                skill_id=entry.skill_id,
                name=entry.name,
                version_id=version.id,
                version_label=version.label,
                guide=version.manifest.guide,
                scope=entry.scope,
            )
            for entry, version in await self.active_entries(
                snapshot,
                tuple(item.skill_id for item in snapshot.skills if item.skill_id in active_ids),
            )
        )
        return RunSkillPromptContext(catalog=snapshot.skills, active=active)

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
        entry, version = await self.resolve_run_entry(snapshot, skill_id)
        if entry.scope is SkillScope.PERSONAL:
            # 个人 Skill 只提供 Markdown 指南，任何脚本调用都按未登记目标拒绝。
            raise SkillNotFoundError("skill_script_not_registered")
        script = next(
            (item for item in version.manifest.scripts if item.path == script_path),
            None,
        )
        if script is None:
            raise SkillNotFoundError("skill_script_not_registered")
        validation_error = validate_parameter_schema(script.parameters, parameters)
        if validation_error is not None:
            raise ValueError("skill_parameters_invalid")
        version, dependency_check = await self.recheck_version_dependencies(
            version,
            use_cached_dependency_check=use_cached_dependency_check,
        )
        if dependency_check.status is not DependencyCheckStatus.SATISFIED:
            raise SkillDependencyUnsatisfiedError()
        return entry, version, script, dependency_check

    async def active_entries(
        self,
        snapshot: RunSkillSnapshot,
        active_skill_ids: tuple[str, ...],
    ) -> tuple[tuple[RunSkillEntry, SkillVersion], ...]:
        """按 Run 目录顺序解析选中和成功加载的技能正文。"""
        active = set(active_skill_ids)
        result: list[tuple[RunSkillEntry, SkillVersion]] = []
        for entry in snapshot.skills:
            if entry.skill_id not in active:
                continue
            version = await self._repository.get_version(entry.version_id)
            if version is None or version.skill_id != entry.skill_id:
                raise SkillNotFoundError("skill_version_unavailable")
            result.append((entry, version))
        return tuple(result)

    async def recheck_version_dependencies(
        self,
        version: SkillVersion,
        *,
        use_cached_dependency_check: bool = False,
    ) -> tuple[SkillVersion, DependencyCheck]:
        """执行前复核依赖并仅在实质结果变化时写回数据库，消除 checked_at 导致的写放大。"""
        if use_cached_dependency_check:
            check = await asyncio.to_thread(
                self._dependency_checker.check_cached,
                version.dependency_requirements,
            )
        else:
            check = await asyncio.to_thread(
                self._dependency_checker.check,
                version.dependency_requirements,
                checked_at=self._clock(),
            )
        # 忽略仅 checked_at 推进、状态与包版本未变的探测结果，避免无意义 FOR UPDATE 写事务。
        if _dependency_check_changed(version.dependency_check, check):
            refreshed = await self._repository.record_dependency_check(version.id, check)
            if refreshed is None:
                raise SkillNotFoundError("skill_version_unavailable")
            version = refreshed
        return version, check

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
        if offset < 0 or not 1 <= limit_bytes <= 64 * 1024:
            raise ValueError("skill_resource_range_invalid")
        _, version = await self.resolve_run_entry(snapshot, skill_id)
        resource = next((item for item in version.manifest.files if item.path == path), None)
        if resource is None:
            raise SkillNotFoundError("skill_resource_not_found")
        if not resource.text:
            raise ValueError("skill_resource_not_text")
        if version.object_key is None:
            # 纯 Markdown 版本直接从数据库 manifest.guide 返回 UTF-8 切片（零 MinIO / 零缓存）。
            if path != "SKILL.md":
                raise SkillNotFoundError("skill_resource_not_found")
            content, next_offset, eof, file_size = _slice_utf8_bytes(
                version.manifest.guide.encode("utf-8"),
                offset=offset,
                limit_bytes=limit_bytes,
            )
        else:
            # ZIP 版本统一经内存字节缓存或对象存储获取字节流后走单一 ZIP 读取逻辑。
            object_store = self._require_object_store()
            object_key = version.object_key
            if self._package_cache is not None:
                try:
                    package = await self._package_cache.get_zip_bytes(
                        version,
                        fetch_bytes=lambda: object_store.get_bytes(
                            object_key,
                            maximum_bytes=MAX_PACKAGE_ZIP_BYTES,
                        ),
                    )
                except ValueError as error:
                    if str(error) == "skill_package_integrity_invalid":
                        raise SkillNotFoundError("skill_version_unavailable") from error
                    raise
            else:
                package = await object_store.get_bytes(
                    object_key,
                    maximum_bytes=MAX_PACKAGE_ZIP_BYTES,
                )
            content, next_offset, eof, file_size = await run_thread_shielded(
                _read_resource_slice_from_zip,
                package,
                path,
                offset,
                limit_bytes,
                name="athena-skill-read-resource",
            )
        return SkillResourceSlice(
            skill_id=skill_id,
            skill_version_id=version.id,
            path=path,
            offset=offset,
            next_offset=next_offset,
            content=content,
            eof=eof,
            size_bytes=file_size,
        )

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

    async def _authorize_admin(self, actor: AuthContext) -> None:
        user = await self._auth_reader.get_current_session_user(
            actor.user.id,
            actor.session_id,
            self._clock(),
        )
        if user is None or user.role is not UserRole.SUPER_ADMIN:
            raise SkillAuthorizationError("admin_required")

    async def _authorize_skill_manager(self, actor: AuthContext) -> User:
        """校验当前登录态为活跃超级管理员或部门管理员。"""
        user = await self._auth_reader.get_current_session_user(
            actor.user.id,
            actor.session_id,
            self._clock(),
        )
        if user is None or user.role not in {UserRole.SUPER_ADMIN, UserRole.DEPT_ADMIN}:
            raise SkillAuthorizationError("admin_required")
        return user

    async def _require_manageable_admin_skill(self, actor: AuthContext, skill_id: str) -> Skill:
        """校验管理端对目标 Skill 的访问权：不可读返回 404，可读但不可管理返回 403。"""
        user = await self._authorize_skill_manager(actor)
        skill = await self._repository.get_skill(skill_id)
        if skill is None:
            raise SkillNotFoundError()
        if user.role is UserRole.SUPER_ADMIN:
            return skill
        if self._access_policy is None:
            raise SkillAuthorizationError("admin_required")
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

    async def _replay_after_import_failure(
        self,
        actor: AuthContext,
        *,
        operation: str,
        target_skill_id: str | None,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> SkillImportRequestResult | None:
        """事务异常后读取幂等结果；普通查询失败视为提交状态不明。"""
        try:
            return await self._repository.find_import_result(
                actor,
                operation=operation,
                target_skill_id=target_skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
        except asyncio.CancelledError:
            raise
        except PermissionError as error:
            # 提交结果未知时仍必须优先反映管理员撤权，不能把回查撤权吞成普通失败。
            raise SkillAuthorizationError("admin_required") from error
        except Exception:
            return None

    @staticmethod
    def _package_key(sha256: str, version_id: str) -> str:
        return f"skills/{sha256[:2]}/{sha256}/{version_id}.zip"


def _dependency_check_changed(
    current: DependencyCheck | None,
    fresh: DependencyCheck,
) -> bool:
    """比较依赖检查是否发生实质语义变化；仅 checked_at 刷新不触发数据库写回。"""
    if current is None:
        return True
    return (
        current.status != fresh.status
        or current.python_version != fresh.python_version
        or current.python_implementation != fresh.python_implementation
        or current.items != fresh.items
    )


def _validate_idempotency_key(value: str) -> str:
    """导入请求键只接受可见 ASCII，避免日志注入和无效唯一键。"""
    if not isinstance(value, str) or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise ValueError("idempotency_key_invalid")
    return value


def _sha256_json(payload: dict[str, object]) -> str:
    """对规范化 JSON 对象计算确定性 SHA-256 幂等请求指纹。"""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    return _sha256_json(
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


def _personal_fingerprint(
    *,
    actor: AuthContext,
    operation: str,
    target_skill_id: str | None,
    expected_revision: int | None,
    draft: PersonalSkillDraft,
) -> str:
    """摘要覆盖本人、操作、目标、修订号和完整可编辑正文。"""
    return _sha256_json(
        {
            "operation": operation,
            "owner_id": actor.user.id,
            "target_skill_id": target_skill_id,
            "expected_revision": expected_revision,
            "name": draft.name,
            "description": draft.description,
            "guide": draft.guide,
        }
    )


def _sort_user_directory(items: tuple[Skill, ...]) -> tuple[Skill, ...]:
    """按推荐优先、范围分层（部门 > 公司 > 个人）及更新时间倒序稳定排列用户目录。"""
    return tuple(
        sorted(
            items,
            key=lambda item: (
                not item.featured,
                _DIRECTORY_SCOPE_PRIORITY.get(item.scope, 99),
                -item.updated_at.timestamp(),
                item.slug,
                item.id,
            ),
        )
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
    return _sha256_json(
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
    return _sha256_json(
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


def _validate_utf8_offset_boundary(
    prefix: bytes,
    chunk: bytes,
    *,
    offset: int,
) -> None:
    """严格 O(1) 校验 offset 是否处于合法 UTF-8 字符边界，消除原 O(N²) 全前缀解码。"""
    # 1. 目标分片首字节若是 UTF-8 续字节（10xxxxxx），说明 offset 落在多字节字符中间。
    if chunk and (chunk[0] & 0xC0) == 0x80:
        raise ValueError("skill_resource_offset_invalid")
    # 2. 若 offset > 0，向前回看最多 4 字节，验证紧贴 offset 结束的 UTF-8 字符完整且合法
    #    （覆盖 4 字节 UTF-8 字符、offset == file_size 及损坏/截断字节边界）。
    if offset > 0:
        if not prefix:
            raise ValueError("skill_resource_offset_invalid")
        lead_index = len(prefix) - 1
        while lead_index >= 0 and (prefix[lead_index] & 0xC0) == 0x80:
            lead_index -= 1
        if lead_index < 0:
            raise ValueError("skill_resource_offset_invalid")
        try:
            prefix[lead_index:].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("skill_resource_offset_invalid") from error


def _decode_utf8_resource_slice(
    prefix: bytes,
    chunk: bytes,
    *,
    offset: int,
    file_size: int,
) -> tuple[str, int, bool, int]:
    """执行 O(1) 起始边界校验与末尾最多 3 字节多字节回退解码。"""
    _validate_utf8_offset_boundary(prefix, chunk, offset=offset)
    requested_nonempty = bool(chunk)
    # 分段末尾若落在多字节字符中间，只回退到上一个完整字符边界。
    while chunk:
        try:
            content = chunk.decode("utf-8")
            break
        except UnicodeDecodeError as error:
            if error.reason == "unexpected end of data" and error.end == len(chunk):
                chunk = chunk[: error.start]
                continue
            raise ValueError("skill_resource_offset_invalid") from error
    else:
        content = ""
    if requested_nonempty and not chunk:
        raise ValueError("skill_resource_range_invalid")
    next_offset = offset + len(chunk)
    return content, next_offset, next_offset >= file_size, file_size


def _slice_utf8_bytes(
    data: bytes,
    *,
    offset: int,
    limit_bytes: int,
) -> tuple[str, int, bool, int]:
    """对完整 UTF-8 字节串按偏移切片并复用 O(1) 字符边界校验。"""
    if len(data) > MAX_PACKAGE_ZIP_BYTES:
        raise ValueError("skill_resource_too_large")
    if offset > len(data):
        raise ValueError("skill_resource_offset_invalid")
    prefix = data[max(0, offset - 4) : offset]
    chunk = data[offset : offset + limit_bytes]
    return _decode_utf8_resource_slice(
        prefix,
        chunk,
        offset=offset,
        file_size=len(data),
    )


def _read_resource_slice_from_zip(
    package: bytes,
    path: str,
    offset: int,
    limit_bytes: int,
) -> tuple[str, int, bool, int]:
    """在工作线程校验路径安全后读取 ZIP 成员并复用 O(1) UTF-8 边界校验。"""
    try:
        safe_relative_posix_parts(path)
    except ValueError as error:
        raise SkillNotFoundError("skill_version_unavailable") from error
    try:
        with zipfile.ZipFile(BytesIO(package)) as archive:
            member = archive.getinfo(path)
            if member.file_size > MAX_PACKAGE_ZIP_BYTES:
                raise ValueError("skill_resource_too_large")
            # 流式有界读取，防止伪造 ZIP header 中 file_size 字段造成解压膨胀。
            with archive.open(member, "r") as stream:
                data = stream.read(MAX_PACKAGE_ZIP_BYTES + 1)
            if len(data) > MAX_PACKAGE_ZIP_BYTES:
                raise ValueError("skill_resource_too_large")
    except KeyError as error:
        raise SkillNotFoundError("skill_resource_not_found") from error
    except zipfile.BadZipFile as error:
        raise SkillNotFoundError("skill_version_unavailable") from error
    return _slice_utf8_bytes(data, offset=offset, limit_bytes=limit_bytes)


def _personal_slug(skill_id: str) -> str:
    """由稳定内部 ID 生成不随名称变化且不暴露名称的 slug。"""
    digest = hashlib.sha256(skill_id.encode("utf-8")).hexdigest()[:32]
    return f"personal-{digest}"


__all__ = [
    "SkillAuthorizationError",
    "SkillPackageError",
    "SkillService",
    "SkillUserAuthorizationError",
]
