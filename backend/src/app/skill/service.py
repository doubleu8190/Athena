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
    SkillScriptDefinition,
    SkillVersion,
    SkillVersionPage,
)


class SkillAuthorizationError(PermissionError):
    """管理操作未通过事务外的当前管理员预检。"""


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

    async def get_version(
        self, actor: AuthContext, skill_id: str, version_id: str
    ) -> SkillVersion:
        """读取目标 Skill 下不可变版本的完整正文。"""
        await self._authorize_admin(actor)
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
                if replay.version.object_key != object_key:
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
                if replay.version.object_key != object_key:
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
        candidates = await self._repository.get_public_directory()
        available: list[Skill] = []
        for skill in candidates:
            if skill.current_version_id is None:
                continue
            version = await self._repository.get_version(skill.current_version_id)
            if version is None or version.skill_id != skill.id:
                continue
            check = await asyncio.to_thread(
                self._dependency_checker.check_cached,
                version.dependency_requirements,
            )
            if check != version.dependency_check:
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


__all__ = ["SkillAuthorizationError", "SkillPackageError", "SkillService"]
