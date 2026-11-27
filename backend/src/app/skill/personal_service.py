"""个人 Markdown Skill 领域用例、幂等重放恢复与共享 Markdown 写事务辅助。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from app.skill.personal import (
    PersonalSkillDraft,
    build_markdown_manifest,
    parse_personal_markdown,
    validate_personal_content,
)
from app.skill.results import SkillAdminDetails
from common.date import utc_now
from common.ids import new_id
from domain.auth import AuthContext
from domain.skill import (
    DependencyCheck,
    DependencyCheckStatus,
    Skill,
    SkillConflictError,
    SkillImportRequestResult,
    SkillNotFoundError,
    SkillRepository,
)

_IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{1,128}$")


class SkillUserAuthorizationError(PermissionError):
    """个人 Skill 写事务发现当前登录账号或会话失效。"""


def validate_idempotency_key(value: str) -> str:
    """导入与发版请求键只接受可见 ASCII，避免日志注入和无效唯一键。"""
    if not isinstance(value, str) or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise ValueError("idempotency_key_invalid")
    return value


def sha256_json(payload: dict[str, object]) -> str:
    """对规范化 JSON 对象计算确定性 SHA-256 幂等请求指纹。"""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def single_version_details(result: SkillImportRequestResult) -> SkillAdminDetails:
    """将单版本写入或幂等重放结果包装为详情视图。"""
    return SkillAdminDetails(result.skill, (result.version,))


def markdown_dependency_check(checked_at: datetime) -> DependencyCheck:
    """为无依赖、无脚本的纯 Markdown 技能生成确定性的免探测结论。"""
    return DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version="not-checked",
        python_implementation="markdown-only",
        items=(),
        checked_at=checked_at,
    )


async def commit_markdown_write[T](
    *,
    personal: bool,
    write: Callable[[], Awaitable[T | None]],
    find_replay: Callable[[], Awaitable[SkillImportRequestResult | None]],
    on_permission_error: Callable[[PermissionError], PermissionError],
    auth_error_types: tuple[type[Exception], ...],
    replay_to_details: Callable[[SkillImportRequestResult], Awaitable[SkillAdminDetails]],
    to_details: Callable[[T], SkillAdminDetails] | None = None,
    to_details_async: Callable[[T], Awaitable[SkillAdminDetails]] | None = None,
    map_slug_exists_to_conflict: bool = False,
) -> SkillAdminDetails:
    """统一执行纯 Markdown 写路径的单库事务与失败后幂等重放恢复。"""
    try:
        written = await write()
    except asyncio.CancelledError:
        raise
    except PermissionError as error:
        raise on_permission_error(error) from error
    except Exception as error:
        replay = await _replay_after_failure(
            personal=personal,
            find_replay=find_replay,
            auth_error_types=auth_error_types,
        )
        if replay is not None:
            return await replay_to_details(replay)
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


async def _replay_after_failure(
    *,
    personal: bool,
    find_replay: Callable[[], Awaitable[SkillImportRequestResult | None]],
    auth_error_types: tuple[type[Exception], ...],
) -> SkillImportRequestResult | None:
    """数据库异常后尝试读取已提交幂等结果；优先透传取消与撤权，普通查询失败视为提交态不明。"""
    try:
        return await find_replay()
    except asyncio.CancelledError:
        raise
    except auth_error_types:
        # 提交结果未知时仍必须优先反映管理员或用户会话撤权，不吞回普通失败。
        raise
    except (SkillConflictError, SkillNotFoundError):
        # 个人恢复查询已给出明确业务结论（例如已删 404 或键冲突）时直接上抛，
        # 避免吞掉 404 再暴露原始数据库异常；管理端冲突恢复返回 None 交由外层处理。
        if personal:
            raise
        return None
    except Exception:
        return None


def _personal_fingerprint(
    *,
    actor: AuthContext,
    operation: str,
    target_skill_id: str | None,
    expected_revision: int | None,
    draft: PersonalSkillDraft,
) -> str:
    """摘要覆盖本人、操作、目标、修订号和完整可编辑正文。"""
    return sha256_json(
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


def _personal_slug(skill_id: str) -> str:
    """由稳定内部 ID 生成不随名称变化且不暴露名称的 slug。"""
    digest = hashlib.sha256(skill_id.encode("utf-8")).hexdigest()[:32]
    return f"personal-{digest}"


class PersonalSkillService:
    """承接用户个人纯 Markdown Skill 的创建、发版、启停与软删除用例。"""

    def __init__(
        self,
        repository: SkillRepository,
        *,
        id_factory: Callable[[str], str] = new_id,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._id_factory = id_factory
        self._clock = clock

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
        idempotency_key = validate_idempotency_key(idempotency_key)
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
            return single_version_details(replay)

        skill_id = self._id_factory("skill")
        version_id = self._id_factory("skillver")
        manifest, package_sha256 = build_markdown_manifest(_personal_slug(skill_id), draft)
        # 纯 Markdown 个人技能走纯数据库单事务，零 ZIP 构造、零 MinIO 依赖。
        return await commit_markdown_write(
            personal=True,
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
            find_replay=lambda: self._find_personal_replay(
                actor,
                operation="personal_create",
                target_skill_id=None,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
            ),
            on_permission_error=lambda _error: SkillUserAuthorizationError("not_authenticated"),
            auth_error_types=(SkillUserAuthorizationError,),
            replay_to_details=_async_single_version_details,
            to_details=single_version_details,
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
        idempotency_key = validate_idempotency_key(idempotency_key)
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
            return single_version_details(replay)
        skill = await self._repository.get_personal_skill(actor.user.id, skill_id)
        if skill is None:
            raise SkillNotFoundError()
        if skill.revision != expected_revision:
            raise SkillConflictError("skill_revision_conflict")

        version_id = self._id_factory("skillver")
        manifest, package_sha256 = build_markdown_manifest(skill.slug, draft)
        return await commit_markdown_write(
            personal=True,
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
            find_replay=lambda: self._find_personal_replay(
                actor,
                operation="personal_publish",
                target_skill_id=skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
            ),
            on_permission_error=lambda _error: SkillUserAuthorizationError("not_authenticated"),
            auth_error_types=(SkillUserAuthorizationError,),
            replay_to_details=_async_single_version_details,
            to_details=single_version_details,
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
        return markdown_dependency_check(self._clock())


async def _async_single_version_details(
    result: SkillImportRequestResult,
) -> SkillAdminDetails:
    """将同步单版本结果包装为异步重放详情适配器。"""
    return single_version_details(result)


__all__ = [
    "PersonalSkillService",
    "SkillUserAuthorizationError",
    "commit_markdown_write",
    "markdown_dependency_check",
    "sha256_json",
    "single_version_details",
    "validate_idempotency_key",
]
