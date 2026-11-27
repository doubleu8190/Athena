"""将会话消息领域对象转换为 HTTP DTO。"""

from __future__ import annotations

from api.dto.message import (
    ConversationMessageListResponse,
    ConversationMessageResponse,
)
from domain.message import ConversationMessage, ConversationMessagePage


def conversation_message_to_response(
    message: ConversationMessage,
) -> ConversationMessageResponse:
    """按 DTO 字段白名单投影单条会话消息，自动过滤内部 metadata 属性。"""
    return ConversationMessageResponse.model_validate(message)


def conversation_page_to_response(
    page: ConversationMessagePage,
) -> ConversationMessageListResponse:
    """转换会话消息分页。"""
    return ConversationMessageListResponse.model_validate(page)


__all__ = ["conversation_message_to_response", "conversation_page_to_response"]
