"""会话历史只读查询用例。"""

from __future__ import annotations

from app.message.converter.result import conversation_page_to_result
from app.message.model.result import ConversationMessagePageResult
from domain.message import (
    InvalidMessageError,
    MessageRepository,
    SessionNotFoundError,
)

MAX_PAGE_SIZE = 100
MAX_IDENTIFIER_LENGTH = 128
MAX_MESSAGE_ID_LENGTH = 64


def _require_identifier(
    value: str,
    error: str,
    *,
    max_length: int = MAX_IDENTIFIER_LENGTH,
) -> str:
    """校验必填标识符。"""
    if not isinstance(value, str) or not value or not value.strip() or len(value) > max_length:
        raise InvalidMessageError(error)
    return value


class MessageService:
    """提供按所有者隔离的公开会话历史分页查询（写入统一经由 Run 事务入口执行）。"""

    def __init__(self, repository: MessageRepository) -> None:
        """保存会话消息只读仓储。"""
        self._repository = repository

    async def list_conversation(
        self,
        session_id: str,
        *,
        viewer_id: str,
        before_sequence: int | None = None,
        limit: int = 20,
    ) -> ConversationMessagePageResult:
        """按所有者读取会话历史，不返回执行轨迹。"""
        _require_identifier(
            session_id,
            "invalid_session_id",
            max_length=MAX_MESSAGE_ID_LENGTH,
        )
        _require_identifier(viewer_id, "invalid_viewer_id")
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise InvalidMessageError("invalid_pagination")
        if before_sequence is not None and before_sequence < 1:
            raise InvalidMessageError("invalid_before_sequence")
        page = await self._repository.list_conversation_for_owner(
            session_id,
            viewer_id,
            before_sequence=before_sequence,
            limit=limit,
        )
        if page is None:
            raise SessionNotFoundError("session_not_found")
        return conversation_page_to_result(page)


__all__ = ["MAX_PAGE_SIZE", "MessageService"]
