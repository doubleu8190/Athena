"""面向用户目录与 Run 执行期的 Skill 只读目录、快照、依赖复核与资源切片读取。"""

from __future__ import annotations

import asyncio
import zipfile
from collections.abc import Callable, Sequence
from datetime import datetime
from io import BytesIO

from app.skill.dependency import DependencyChecker
from app.skill.package import validate_parameter_schema
from app.skill.package_cache import SkillPackageCache, safe_relative_posix_parts
from app.skill.results import SkillResourceSlice
from common.async_utils import run_thread_shielded
from common.date import utc_now
from domain.auth import AuthContext
from domain.file import FileObjectStore, FileStorageError
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
    SkillFormat,
    SkillNotFoundError,
    SkillRepository,
    SkillScope,
    SkillScriptDefinition,
    SkillVersion,
)

# 用户可用目录排序优先级：推荐置顶优先，其后按部门 > 公司 > 个人分层排序。
_DIRECTORY_SCOPE_PRIORITY: dict[SkillScope, int] = {
    SkillScope.DEPARTMENT: 0,
    SkillScope.COMPANY: 1,
    SkillScope.PERSONAL: 2,
}


def is_plain_markdown_skill(skill: Skill, version: SkillVersion) -> bool:
    """判断是否为无脚本、无依赖的纯 Markdown 技能（免于触发 Python 解释器探测）。"""
    return (
        skill.format is SkillFormat.MARKDOWN
        and not version.dependency_requirements
        and not version.manifest.scripts
    )


def require_object_store(object_store: FileObjectStore | None) -> FileObjectStore:
    """对象存储未启用时拒绝 ZIP 导入、ZIP 资源读取与脚本日志下载，统一映射 503。"""
    if object_store is None:
        raise FileStorageError("file_storage_unavailable")
    return object_store


async def check_dependencies(
    dependency_checker: DependencyChecker,
    requirements: Sequence[str],
    *,
    clock: Callable[[], datetime],
    cached: bool = False,
) -> DependencyCheck:
    """在工作线程执行目标环境依赖探测，可选走只读缓存避免 GET 目录高频探测。"""
    if cached:
        return await asyncio.to_thread(dependency_checker.check_cached, requirements)
    return await asyncio.to_thread(
        dependency_checker.check,
        requirements,
        checked_at=clock(),
    )


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


def _validate_utf8_offset_boundary(
    prefix: bytes,
    chunk: bytes,
    *,
    offset: int,
) -> None:
    """严格 O(1) 校验 offset 是否处于合法 UTF-8 字符边界，消除全前缀解码开销。"""
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


class SkillRuntimeReader:
    """承接面向用户目录与 Run 执行期的 Skill 只读目录、快照与资源切片读取。"""

    def __init__(
        self,
        repository: SkillRepository,
        object_store: FileObjectStore | None,
        *,
        dependency_checker: DependencyChecker,
        package_cache: SkillPackageCache | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._object_store = object_store
        self._dependency_checker = dependency_checker
        self._package_cache = package_cache
        self._clock = clock

    async def _load_readable_user_entries(
        self,
        user_id: str,
    ) -> tuple[tuple[Skill, SkillVersion], ...]:
        """加载用户实时可读的 (Skill, 当前 SkillVersion) 候选集。"""
        return await self._repository.get_readable_user_directory_entries(user_id)

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
            if is_plain_markdown_skill(skill, version):
                available.append(skill)
                continue
            # GET 目录仅做只读实时过滤，严禁在此触发 record_dependency_check 写库；
            # 依赖检查持久化仅发生在导入、新增版本、配置修改与执行前显式复核。
            check = await check_dependencies(
                self._dependency_checker,
                version.dependency_requirements,
                clock=self._clock,
                cached=True,
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
        check = await check_dependencies(
            self._dependency_checker,
            version.dependency_requirements,
            clock=self._clock,
            cached=use_cached_dependency_check,
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
            object_store = require_object_store(self._object_store)
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


__all__ = [
    "SkillRuntimeReader",
    "_decode_utf8_resource_slice",
    "check_dependencies",
    "is_plain_markdown_skill",
    "require_object_store",
]
