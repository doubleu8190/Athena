"""将消息领域对象转换为应用结果。"""

from __future__ import annotations

from app.message.model.result import (
    ConversationMessagePageResult,
    ConversationMessageResult,
)
from domain.message import ConversationMessage, ConversationMessagePage


def conversation_message_to_result(
    message: ConversationMessage,
) -> ConversationMessageResult:
    """隐藏消息内部元数据并生成稳定应用结果。"""
    return ConversationMessageResult(
        id=message.id,
        session_id=message.session_id,
        role=message.role,
        status=message.status,
        sequence=message.sequence,
        parts=message.parts,
        reply_to_message_id=message.reply_to_message_id,
        run_id=message.run_id,
        created_at=message.created_at,
        completed_at=message.completed_at,
    )


def conversation_page_to_result(
    page: ConversationMessagePage,
) -> ConversationMessagePageResult:
    """批量转换会话消息分页。"""
    return ConversationMessagePageResult(
        items=tuple(conversation_message_to_result(item) for item in page.items),
        next_before_sequence=page.next_before_sequence,
    )


__all__ = ["conversation_message_to_result", "conversation_page_to_result"]
