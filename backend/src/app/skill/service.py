"""Skill 包导入、公开目录、版本切换和资源读取用例。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import zipfile
from collections.abc import Callable
from datetime import datetime
from io import BytesIO
from pathlib import Path

from app.skill.dependency import DependencyChecker
from app.skill.model import SkillAdminDetails, SkillResourceSlice
from app.skill.package import SkillPackageValidator, validate_parameter_schema
from app.skill.personal import (
    PersonalSkillDraft,
    PersonalSkillPackage,
    build_personal_package,
    parse_personal_markdown,
    validate_personal_content,
)
from common.ids import new_id, now_utc
from domain.auth import AuthContext, CurrentSessionReader, UserRole
from domain.file import FileObjectStore
from domain.skill import (
    MAX_PACKAGE_ZIP_BYTES,
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
    SkillImportRequestResult,
    SkillNotFoundError,
    SkillPackageError,
    SkillPackageManifest,
    SkillRepository,
    SkillScope,
    SkillScriptDefinition,
    SkillVersion,
    SkillVersionPage,
)


class SkillAuthorizationError(PermissionError):
    """管理操作未通过事务外的当前管理员预检。"""


class SkillUserAuthorizationError(PermissionError):
    """个人 Skill 写事务发现当前登录账号或会话失效。"""


_IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{1,128}$")


class SkillService:
    """管理不可变包版本，并在目标环境复核依赖。"""

    def __init__(
        self,
        repository: SkillRepository,
        object_store: FileObjectStore,
        auth_reader: CurrentSessionReader,
        *,
        package_validator: SkillPackageValidator | None = None,
        dependency_checker: DependencyChecker | None = None,
        id_factory: Callable[[str], str] = new_id,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """保存持久化、私有对象存储、认证和目标环境检查依赖。"""
        self._repository = repository
        self._object_store = object_store
        self._auth_reader = auth_reader
        self._package_validator = package_validator or SkillPackageValidator()
        self._dependency_checker = dependency_checker or DependencyChecker(clock=clock)
        self._id_factory = id_factory
        self._clock = clock

    async def list_admin(self, actor: AuthContext) -> tuple[Skill, ...]:
        """读取全部管理目录项。"""
        await self._authorize_admin(actor)
        return await self._repository.list_skills()

    async def get_admin(self, actor: AuthContext, skill_id: str) -> SkillAdminDetails:
        """读取管理员详情和历史版本。"""
        await self._authorize_admin(actor)
        skill = await self._repository.get_skill(skill_id)
        if skill is None:
            raise SkillNotFoundError()
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
        package = build_personal_package(_personal_slug(skill_id), draft)
        return await self._publish_personal_create(
            actor,
            skill_id=skill_id,
            version_id=version_id,
            package_content=package.content,
            package=package,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
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
        package = build_personal_package(skill.slug, draft)
        return await self._publish_personal_version(
            actor,
            skill_id=skill_id,
            version_id=version_id,
            expected_revision=expected_revision,
            package_content=package.content,
            package=package,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
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

    async def get_version(
        self, actor: AuthContext, skill_id: str, version_id: str
    ) -> SkillVersion:
        """读取目标 Skill 下不可变版本的完整正文。"""
        await self._authorize_admin(actor)
        skill = await self._repository.get_skill(skill_id)
        if skill is None:
            raise SkillNotFoundError()
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
        await self._authorize_admin(actor)
        result = await self._repository.list_versions(
            skill_id,
            page=page,
            page_size=page_size,
        )
        if result is None:
            raise SkillNotFoundError()
        return result

    async def import_package(
        self,
        actor: AuthContext,
        path: Path,
        *,
        idempotency_key: str,
    ) -> SkillAdminDetails:
        """首发导入完整 ZIP；成功版本保持未启用且不切换指针。"""
        await self._authorize_admin(actor)
        idempotency_key = _validate_idempotency_key(idempotency_key)
        validated = self._package_validator.validate(path)
        fingerprint = _import_fingerprint(
            actor=actor,
            operation="import",
            target_skill_id=None,
            package_sha256=validated.package_sha256,
            label=None,
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
        )

    async def _publish_personal_create(
        self,
        actor: AuthContext,
        *,
        skill_id: str,
        version_id: str,
        package_content: bytes,
        package: PersonalSkillPackage,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> SkillAdminDetails:
        """先写不可变对象，再原子创建个人身份并处理不确定提交。"""
        object_key = self._package_key(package.package_sha256, version_id)
        try:
            await self._object_store.put_bytes(
                object_key,
                package_content,
                content_type="application/zip",
            )
        except BaseException:
            await self._discard_prepared_package(object_key)
            raise
        try:
            result = await self._repository.create_personal_skill(
                skill_id=skill_id,
                version_id=version_id,
                manifest=package.manifest,
                object_key=object_key,
                package_sha256=package.package_sha256,
                dependency_check=self._personal_dependency_check(),
                actor=actor,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                created_at=self._clock(),
            )
        except asyncio.CancelledError:
            # 取消可能发生在数据库提交之后，对象必须保留给重放或离线回收。
            raise
        except PermissionError as error:
            raise SkillUserAuthorizationError("not_authenticated") from error
        except SkillConflictError:
            replay = await self._replay_personal_after_failure(
                actor,
                operation="personal_create",
                target_skill_id=None,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                if (replay.request_version or replay.version).object_key != object_key:
                    await self._discard_prepared_package(object_key)
                return SkillAdminDetails(replay.skill, (replay.version,))
            await self._discard_prepared_package(object_key)
            raise
        except Exception:
            replay = await self._replay_personal_after_failure(
                actor,
                operation="personal_create",
                target_skill_id=None,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                if (replay.request_version or replay.version).object_key != object_key:
                    await self._discard_prepared_package(object_key)
                return SkillAdminDetails(replay.skill, (replay.version,))
            # 未命中不能证明数据库不会稍后提交，保留对象供幂等重放与安全回收。
            raise
        if result.version.object_key != object_key:
            await self._discard_prepared_package(object_key)
        return SkillAdminDetails(result.skill, (result.version,))

    async def _publish_personal_version(
        self,
        actor: AuthContext,
        *,
        skill_id: str,
        version_id: str,
        expected_revision: int,
        package_content: bytes,
        package: PersonalSkillPackage,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> SkillAdminDetails:
        """保存个人版本对象，再以 CAS 发布并对不确定提交做安全重放。"""
        object_key = self._package_key(package.package_sha256, version_id)
        try:
            await self._object_store.put_bytes(
                object_key,
                package_content,
                content_type="application/zip",
            )
        except BaseException:
            await self._discard_prepared_package(object_key)
            raise
        try:
            result = await self._repository.publish_personal_version(
                skill_id,
                version_id=version_id,
                expected_revision=expected_revision,
                manifest=package.manifest,
                object_key=object_key,
                package_sha256=package.package_sha256,
                dependency_check=self._personal_dependency_check(),
                actor=actor,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                created_at=self._clock(),
            )
        except asyncio.CancelledError:
            raise
        except PermissionError as error:
            raise SkillUserAuthorizationError("not_authenticated") from error
        except (SkillConflictError, SkillNotFoundError):
            replay = await self._replay_personal_after_failure(
                actor,
                operation="personal_publish",
                target_skill_id=skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                if (replay.request_version or replay.version).object_key != object_key:
                    await self._discard_prepared_package(object_key)
                return SkillAdminDetails(replay.skill, (replay.version,))
            await self._discard_prepared_package(object_key)
            raise
        except Exception:
            replay = await self._replay_personal_after_failure(
                actor,
                operation="personal_publish",
                target_skill_id=skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                if (replay.request_version or replay.version).object_key != object_key:
                    await self._discard_prepared_package(object_key)
                return SkillAdminDetails(replay.skill, (replay.version,))
            raise
        if result is None:
            await self._discard_prepared_package(object_key)
            raise SkillNotFoundError()
        if result.version.object_key != object_key:
            await self._discard_prepared_package(object_key)
        return SkillAdminDetails(result.skill, (result.version,))

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
    ) -> SkillAdminDetails:
        """先写入首发包，再提交 Skill 身份和版本引用。"""
        skill_id = self._id_factory("skill")
        version_id = self._id_factory("skillver")
        object_key = self._package_key(package_sha256, version_id)
        try:
            await self._object_store.put_file(
                object_key,
                path,
                content_type="application/zip",
            )
        except BaseException:
            await self._discard_prepared_package(object_key)
            raise
        try:
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
            )
            if version.id != version_id:
                # 并发首发由唯一约束选出胜者，败者只清理自己写入的对象。
                await self._discard_prepared_package(object_key)
                return SkillAdminDetails(skill, (version,))
        except asyncio.CancelledError:
            # 取消可能发生在提交后的响应阶段，保留对象供幂等重放或安全回收。
            raise
        except PermissionError as error:
            # 事务内管理员复核失败统一映射为 403；对象可能已被引用或提交状态不明，
            # 因此不能按普通失败路径清理。
            raise SkillAuthorizationError("admin_required") from error
        except SkillConflictError:
            replay = await self._replay_after_import_failure(
                actor,
                operation="import",
                target_skill_id=None,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                if (replay.request_version or replay.version).object_key != object_key:
                    await self._discard_prepared_package(object_key)
                return SkillAdminDetails(replay.skill, (replay.version,))
            # 适配器只会把已回滚的唯一约束冲突收敛为该业务异常。
            await self._discard_prepared_package(object_key)
            raise
        except Exception:
            replay = await self._replay_after_import_failure(
                actor,
                operation="import",
                target_skill_id=None,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                return SkillAdminDetails(replay.skill, (replay.version,))
            # 临时未命中不能证明仍在提交的事务不会稍后提交，必须保留对象。
            raise
        return SkillAdminDetails(skill, (version,))

    async def add_version(
        self,
        actor: AuthContext,
        skill_id: str,
        path: Path,
        *,
        idempotency_key: str,
        label: str | None = None,
    ) -> SkillVersion:
        """上传完整更新包；slug 不符时不写数据库、不改变当前指针。"""
        await self._authorize_admin(actor)
        idempotency_key = _validate_idempotency_key(idempotency_key)
        try:
            skill = await self._repository.get_skill(skill_id)
        except PermissionError as error:
            # add_version 的初始目标读取也可能在事务内发现管理员撤权。
            raise SkillAuthorizationError("admin_required") from error
        if skill is None:
            raise SkillNotFoundError()
        validated = self._package_validator.validate(path)
        if validated.manifest.slug != skill.slug:
            raise SkillConflictError("skill_slug_mismatch")
        fingerprint = _import_fingerprint(
            actor=actor,
            operation="add_version",
            target_skill_id=skill_id,
            package_sha256=validated.package_sha256,
            label=label,
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
            return replay.version
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
        object_key = self._package_key(package_sha256, version_id)
        try:
            await self._object_store.put_file(
                object_key,
                path,
                content_type="application/zip",
            )
        except BaseException:
            await self._discard_prepared_package(object_key)
            raise
        try:
            version = await self._repository.add_version(
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
            )
        except asyncio.CancelledError:
            raise
        except PermissionError as error:
            # 与 configure 一致，事务内授权失效必须稳定映射为管理员 403。
            raise SkillAuthorizationError("admin_required") from error
        except SkillConflictError:
            replay = await self._replay_after_import_failure(
                actor,
                operation="add_version",
                target_skill_id=skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                if (replay.request_version or replay.version).object_key != object_key:
                    await self._discard_prepared_package(object_key)
                return replay.version
            await self._discard_prepared_package(object_key)
            raise
        except Exception:
            replay = await self._replay_after_import_failure(
                actor,
                operation="add_version",
                target_skill_id=skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                return replay.version
            # 未知异常与取消相同：无法确认是否已提交时保留对象。
            raise
        if version is None:
            await self._discard_prepared_package(object_key)
            raise SkillNotFoundError()
        if version.id != version_id:
            await self._discard_prepared_package(object_key)
        return version

    async def configure(
        self,
        actor: AuthContext,
        skill_id: str,
        *,
        expected_revision: int,
        enabled: bool,
        current_version_id: str,
        script_requires_approval: bool,
    ) -> Skill:
        """按目标环境最新检查结果 CAS 切换版本、启停和审批。"""
        await self._authorize_admin(actor)
        version = await self._repository.get_version(current_version_id)
        if version is None or version.skill_id != skill_id:
            raise SkillNotFoundError("skill_version_not_found")
        if enabled:
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
            )
        except PermissionError as error:
            # 事务内会再次锁定当前登录态；管理员失效统一收敛为 403 业务异常。
            raise SkillAuthorizationError("admin_required") from error
        if result is None:
            raise SkillNotFoundError()
        return result

    async def public_directory(self) -> tuple[Skill, ...]:
        """返回实时满足当前解释器依赖的已启用 Skill。"""
        # 优先通过批量 JOIN 接口一次读取 Skill 与当前版本，未实现时回退旧接口以兼容测试替身。
        if callable(getattr(self._repository, "get_public_directory_entries", None)):
            entries = await self._repository.get_public_directory_entries()
            return await self._available_directory_entries(entries)
        candidates = await self._repository.get_public_directory()
        return await self._available_directory(candidates)

    async def user_directory(self, actor: AuthContext) -> tuple[Skill, ...]:
        """返回公共及当前用户本人的启用可用 Skill。"""
        if callable(getattr(self._repository, "get_user_directory_entries", None)):
            entries = await self._repository.get_user_directory_entries(actor.user.id)
            return await self._available_directory_entries(entries)
        candidates = await self._repository.get_user_directory(actor.user.id)
        return await self._available_directory(candidates)

    async def _available_directory(
        self,
        candidates: tuple[Skill, ...],
    ) -> tuple[Skill, ...]:
        """降级路径：逐项补齐当前版本后复用统一目录过滤逻辑。"""
        entries: list[tuple[Skill, SkillVersion]] = []
        for skill in candidates:
            if skill.current_version_id is None:
                continue
            version = await self._repository.get_version(skill.current_version_id)
            if version is None or version.skill_id != skill.id:
                continue
            entries.append((skill, version))
        return await self._available_directory_entries(tuple(entries))

    async def _available_directory_entries(
        self,
        entries: tuple[tuple[Skill, SkillVersion], ...],
    ) -> tuple[Skill, ...]:
        """过滤失效公共依赖，个人 Markdown 不触发解释器探测；仅依赖语义变更时才写库。"""
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
            check = await asyncio.to_thread(
                self._dependency_checker.check_cached,
                version.dependency_requirements,
            )
            # 忽略仅因 60s 探测缓存 TTL 刷新而变动的 checked_at，
            # 消除 GET 目录周期性 FOR UPDATE 写放大。
            if _dependency_check_changed(version.dependency_check, check):
                await self._repository.record_dependency_check(version.id, check)
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
                tuple(
                    item.skill_id
                    for item in snapshot.skills
                    if item.skill_id in active_ids
                ),
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
    ) -> tuple[
        RunSkillEntry,
        SkillVersion,
        SkillScriptDefinition,
        DependencyCheck,
    ]:
        """解析固定版本，并返回已刷新到当前 Worker 环境的依赖结论。"""
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
        version, dependency_check = await self.recheck_version_dependencies(version)
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
    ) -> tuple[SkillVersion, DependencyCheck]:
        """执行前复核并显式返回本次结论及可能刷新后的版本。"""
        check = await asyncio.to_thread(
            self._dependency_checker.check,
            version.dependency_requirements,
            checked_at=self._clock(),
        )
        if check != version.dependency_check:
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
        """从 Run 固定版本 ZIP 读取文本资源并保持 UTF-8 字符边界。"""
        if offset < 0 or not 1 <= limit_bytes <= 64 * 1024:
            raise ValueError("skill_resource_range_invalid")
        _, version = await self.resolve_run_entry(snapshot, skill_id)
        resource = next((item for item in version.manifest.files if item.path == path), None)
        if resource is None:
            raise SkillNotFoundError("skill_resource_not_found")
        if not resource.text:
            raise ValueError("skill_resource_not_text")
        package = await self._object_store.get_bytes(
            version.object_key,
            maximum_bytes=MAX_PACKAGE_ZIP_BYTES,
        )
        try:
            with zipfile.ZipFile(BytesIO(package)) as archive:
                member = archive.getinfo(path)
                if member.file_size > MAX_PACKAGE_ZIP_BYTES:
                    raise ValueError("skill_resource_too_large")
                data = archive.read(member)
        except KeyError as error:
            raise SkillNotFoundError("skill_resource_not_found") from error
        except zipfile.BadZipFile as error:
            raise SkillNotFoundError("skill_version_unavailable") from error
        if offset > len(data):
            raise ValueError("skill_resource_offset_invalid")
        chunk = data[offset : offset + limit_bytes]
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
        if offset > 0:
            try:
                data[:offset].decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("skill_resource_offset_invalid") from error
        next_offset = offset + len(chunk)
        return SkillResourceSlice(
            skill_id=skill_id,
            skill_version_id=version.id,
            path=path,
            offset=offset,
            next_offset=next_offset,
            content=content,
            eof=next_offset >= len(data),
            size_bytes=len(data),
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
        return await self._object_store.get_bytes(
            execution.log_object_key,
            maximum_bytes=5 * 1024 * 1024,
        )

    async def _authorize_admin(self, actor: AuthContext) -> None:
        user = await self._auth_reader.get_current_session_user(
            actor.user.id,
            actor.session_id,
            self._clock(),
        )
        if user is None or user.role is not UserRole.ADMIN:
            raise SkillAuthorizationError("admin_required")

    async def _discard_prepared_package(self, object_key: str) -> None:
        """删除已确认未发布的包对象。"""
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


def _import_fingerprint(
    *,
    actor: AuthContext,
    operation: str,
    target_skill_id: str | None,
    package_sha256: str,
    label: str | None,
) -> str:
    """摘要覆盖管理员、操作、目标、包正文和展示标签。"""
    payload = json.dumps(
        {
            "operation": operation,
            "owner_id": actor.user.id,
            "target_skill_id": target_skill_id,
            "package_sha256": package_sha256,
            "label": label,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _personal_fingerprint(
    *,
    actor: AuthContext,
    operation: str,
    target_skill_id: str | None,
    expected_revision: int | None,
    draft: PersonalSkillDraft,
) -> str:
    """摘要覆盖本人、操作、目标、修订号和完整可编辑正文。"""
    payload = json.dumps(
        {
            "operation": operation,
            "owner_id": actor.user.id,
            "target_skill_id": target_skill_id,
            "expected_revision": expected_revision,
            "name": draft.name,
            "description": draft.description,
            "guide": draft.guide,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
