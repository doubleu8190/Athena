"""消息应用层结果模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from domain.message import ConversationRole, MessagePart, MessageStatus


@dataclass(frozen=True, slots=True)
class ConversationMessageResult:
    """不暴露内部 metadata 的会话消息结果。"""

    id: str
    session_id: str
    role: ConversationRole
    status: MessageStatus
    sequence: int
    parts: tuple[MessagePart, ...]
    reply_to_message_id: str | None
    run_id: str | None
    created_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ConversationMessagePageResult:
    """会话消息分页结果。"""

    items: tuple[ConversationMessageResult, ...]
    next_before_sequence: int | None


__all__ = ["ConversationMessagePageResult", "ConversationMessageResult"]
