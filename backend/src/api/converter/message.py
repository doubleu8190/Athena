"""将消息应用结果转换为 HTTP DTO。"""

from __future__ import annotations

from api.dto.message import (
    ConversationMessageListResponse,
    ConversationMessageResponse,
)
from app.message import ConversationMessagePageResult, ConversationMessageResult


def conversation_message_to_response(
    message: ConversationMessageResult,
) -> ConversationMessageResponse:
    """转换单条会话消息（含判别联合 parts 递归属性投影）。"""
    return ConversationMessageResponse.model_validate(message)


def conversation_page_to_response(
    page: ConversationMessagePageResult,
) -> ConversationMessageListResponse:
    """转换会话消息分页。"""
    return ConversationMessageListResponse.model_validate(page)


__all__ = ["conversation_message_to_response", "conversation_page_to_response"]
