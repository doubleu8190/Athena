"""会话目录用例与业务规则。"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime

from app.session.converter.result import session_page_to_result, session_to_result
from app.session.model.result import SessionPageResult, SessionResult
from common.ids import new_id, now_utc
from domain.project import ProjectNotFoundError
from domain.session import (
    InvalidSessionFilterError,
    InvalidSessionTitleError,
    InvalidSessionUpdateError,
    Session,
    SessionNotFoundError,
    SessionRepository,
    SessionScope,
)

MAX_SESSION_TITLE_LENGTH = 120
MAX_SESSION_ID_LENGTH = 64
MAX_PAGE_SIZE = 100


def _default_id_factory() -> str:
    """生成带会话前缀的随机主键。"""
    return new_id("sess")


def _utc_value(value: datetime) -> datetime:
    """把时钟输出规范化为 UTC。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalized_owner_id(owner_id: str) -> str:
    """校验服务端提供的会话所有者。"""
    normalized = owner_id.strip()
    if not normalized:
        raise ValueError("owner_id_required")
    return normalized


def _normalized_session_id(session_id: str) -> str:
    """校验会话标识；非法标识与不存在统一按不可见处理。"""
    if (
        not isinstance(session_id, str)
        or not session_id.strip()
        or len(session_id) > MAX_SESSION_ID_LENGTH
    ):
        raise SessionNotFoundError("session_not_found")
    return session_id


def _normalized_project_id(project_id: str) -> str:
    """校验待绑定的项目标识；非法标识与不存在统一按项目不可见处理。"""
    if (
        not isinstance(project_id, str)
        or not project_id.strip()
        or len(project_id) > MAX_SESSION_ID_LENGTH
    ):
        raise ProjectNotFoundError("project_not_found")
    return project_id


def _normalized_filter_project_id(project_id: str) -> str:
    """校验会话列表的项目筛选值。"""
    if (
        not isinstance(project_id, str)
        or not project_id.strip()
        or len(project_id) > MAX_SESSION_ID_LENGTH
    ):
        raise InvalidSessionFilterError("invalid_session_filter")
    return project_id


def _normalized_title(title: str | None) -> str | None:
    """规范化可选标题并拒绝空白、超长和控制字符。"""
    if title is None:
        return None
    if not isinstance(title, str):
        raise InvalidSessionTitleError("session_title_invalid")
    normalized = title.strip()
    has_control_character = any(unicodedata.category(character) == "Cc" for character in normalized)
    if not normalized or len(normalized) > MAX_SESSION_TITLE_LENGTH or has_control_character:
        raise InvalidSessionTitleError("session_title_invalid")
    return normalized


class SessionService:
    """创建、查询、修改和软删除当前用户的会话。"""

    def __init__(
        self,
        repository: SessionRepository,
        *,
        id_factory: Callable[[], str] = _default_id_factory,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """保存仓储及可替换的 ID、时钟来源。"""
        self._repository = repository
        self._id_factory = id_factory
        self._clock = clock

    async def create(
        self,
        owner_id: str,
        *,
        title: str | None = None,
        project_id: str | None = None,
    ) -> SessionResult:
        """创建未绑定或绑定到当前用户项目的会话。"""
        owner = _normalized_owner_id(owner_id)
        normalized_title = _normalized_title(title)
        normalized_project_id = None if project_id is None else _normalized_project_id(project_id)

        current = _utc_value(self._clock())
        session = await self._repository.create(
            Session(
                id=self._id_factory(),
                owner_id=owner,
                title=normalized_title,
                project_id=normalized_project_id,
                last_message_sequence=0,
                created_at=current,
                updated_at=current,
                deleted_time=None,
            )
        )
        return session_to_result(session)

    async def get(self, owner_id: str, session_id: str) -> SessionResult:
        """读取当前用户可见的未删除会话。"""
        session = await self._repository.get_visible(
            SessionScope(
                session_id=_normalized_session_id(session_id),
                owner_id=_normalized_owner_id(owner_id),
            )
        )
        if session is None:
            raise SessionNotFoundError("session_not_found")
        return session_to_result(session)

    async def list(
        self,
        owner_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
        project_id: str | None = None,
        unbound: bool = False,
    ) -> SessionPageResult:
        """分页读取当前用户的未删除会话。"""
        owner = _normalized_owner_id(owner_id)
        if page < 1 or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError("invalid_pagination")
        if project_id is not None and unbound:
            raise InvalidSessionFilterError("invalid_session_filter")
        normalized_project_id = (
            None if project_id is None else _normalized_filter_project_id(project_id)
        )
        result = await self._repository.list_active(
            owner,
            project_id=normalized_project_id,
            unbound=unbound,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return session_page_to_result(result)

    async def update(
        self,
        owner_id: str,
        session_id: str,
        *,
        update_title: bool = False,
        title: str | None = None,
        update_project: bool = False,
        project_id: str | None = None,
    ) -> SessionResult:
        """修改标题或尚未被首条消息锁定的项目绑定。"""
        if not update_title and not update_project:
            raise InvalidSessionUpdateError("session_update_invalid")
        owner = _normalized_owner_id(owner_id)
        normalized_session_id = _normalized_session_id(session_id)
        scope = SessionScope(session_id=normalized_session_id, owner_id=owner)
        if await self._repository.get_visible(scope) is None:
            raise SessionNotFoundError("session_not_found")
        normalized_title = _normalized_title(title) if update_title else None
        normalized_project_id = (
            _normalized_project_id(project_id)
            if update_project and project_id is not None
            else None
        )
        session = await self._repository.update(
            scope,
            update_title=update_title,
            title=normalized_title,
            update_project=update_project,
            project_id=normalized_project_id,
            updated_at=_utc_value(self._clock()),
        )
        if session is None:
            raise SessionNotFoundError("session_not_found")
        return session_to_result(session)

    async def delete(self, owner_id: str, session_id: str) -> None:
        """软删除当前用户的会话，并允许重复删除。"""
        scope = SessionScope(
            session_id=_normalized_session_id(session_id),
            owner_id=_normalized_owner_id(owner_id),
        )
        deleted = await self._repository.soft_delete(
            scope.owner_id,
            scope.session_id,
            _utc_value(self._clock()),
        )
        if not deleted:
            raise SessionNotFoundError("session_not_found")


__all__ = [
    "MAX_PAGE_SIZE",
    "MAX_SESSION_TITLE_LENGTH",
    "SessionService",
]
