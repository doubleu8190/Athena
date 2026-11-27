"""管理员系统提示词查询、保存与恢复用例。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from app.auth.guard import require_session_roles
from common.date import utc_now
from domain.auth import AuthContext, CurrentSessionReader, UserRole
from domain.system_prompt import (
    MAX_SYSTEM_PROMPT_CONTENT_CHARACTERS,
    SystemPromptAuthorizationError,
    SystemPromptError,
    SystemPromptKind,
    SystemPromptNotFoundError,
    SystemPromptRepository,
    SystemPromptUnavailableError,
    SystemPromptVersion,
    SystemPromptVersionPage,
)

MAX_SYSTEM_PROMPT_PAGE_SIZE = 100


class SystemPromptService:
    """在每次管理操作前复核当前管理员身份并校验正文输入。"""

    def __init__(
        self,
        repository: SystemPromptRepository,
        auth_reader: CurrentSessionReader,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """保存持久化端口、当前会话读取能力和业务时钟。"""
        self._repository = repository
        self._auth_reader = auth_reader
        self._clock = clock

    async def get_current(self, actor: AuthContext, kind: SystemPromptKind) -> SystemPromptVersion:
        """读取指定类别当前正文；初始化配置缺失时返回明确错误。"""
        await self._authorize(actor)
        current = await self._repository.get_current(kind)
        if current is None:
            raise SystemPromptUnavailableError()
        return current

    async def list_versions(
        self,
        actor: AuthContext,
        kind: SystemPromptKind,
        *,
        page: int,
        page_size: int,
    ) -> SystemPromptVersionPage:
        """分页读取指定类别历史元数据。"""
        await self._authorize(actor)
        if page < 1 or not 1 <= page_size <= MAX_SYSTEM_PROMPT_PAGE_SIZE:
            raise SystemPromptError("system_prompt_page_invalid")
        return await self._repository.list_versions(
            kind,
            page=page,
            page_size=page_size,
        )

    async def get_version(
        self, actor: AuthContext, kind: SystemPromptKind, version_id: str
    ) -> SystemPromptVersion:
        """读取同类别历史完整正文。"""
        await self._authorize(actor)
        _validate_version_id(version_id)
        version = await self._repository.get_version(kind, version_id)
        if version is None:
            raise SystemPromptNotFoundError()
        return version

    async def save(
        self,
        actor: AuthContext,
        kind: SystemPromptKind,
        *,
        content: str,
        expected_current_version_id: str,
    ) -> SystemPromptVersion:
        """校验管理员输入并按当前版本 CAS 创建新正文。"""
        await self._authorize(actor)
        _validate_content(content)
        _validate_version_id(expected_current_version_id)
        return await self._repository.save(
            kind,
            content=content,
            expected_current_version_id=expected_current_version_id,
            actor=actor,
            created_at=self._clock(),
        )

    async def restore(
        self,
        actor: AuthContext,
        kind: SystemPromptKind,
        *,
        source_version_id: str,
        expected_current_version_id: str,
    ) -> SystemPromptVersion:
        """从同类别历史正文生成新版本并切换当前指针。"""
        await self._authorize(actor)
        _validate_version_id(source_version_id)
        _validate_version_id(expected_current_version_id)
        return await self._repository.restore(
            kind,
            source_version_id=source_version_id,
            expected_current_version_id=expected_current_version_id,
            actor=actor,
            created_at=self._clock(),
        )

    async def _authorize(self, actor: AuthContext) -> None:
        """通过当前登录会话和数据库角色复核管理员权限。"""
        await require_session_roles(
            self._auth_reader,
            actor,
            frozenset({UserRole.SUPER_ADMIN}),
            self._clock(),
            invalid_session_error=SystemPromptAuthorizationError,
            forbidden_error=SystemPromptAuthorizationError,
        )


def _validate_content(content: str) -> None:
    """拒绝空白或超限正文，同时原样保留换行与可见空白。"""
    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content) > MAX_SYSTEM_PROMPT_CONTENT_CHARACTERS
    ):
        raise SystemPromptError()


def _validate_version_id(version_id: str) -> None:
    """校验版本标识非空且不超过数据库列长度。"""
    if not isinstance(version_id, str) or not version_id or len(version_id) > 128:
        raise SystemPromptError("system_prompt_version_invalid")


__all__ = ["MAX_SYSTEM_PROMPT_PAGE_SIZE", "SystemPromptService"]
