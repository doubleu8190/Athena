"""将消息应用结果转换为 HTTP DTO。"""

from __future__ import annotations

from api.dto.message import (
    ArtifactFileRefResponse,
    ConversationMessageListResponse,
    ConversationMessageResponse,
    FileRefPartResponse,
    MessagePartResponse,
    TextPartResponse,
    ToolCallPartResponse,
    ToolResultPartResponse,
)
from app.message import ConversationMessagePageResult, ConversationMessageResult
from domain.message import FileRefPart, MessagePart, TextPart, ToolCallPart


def _part_to_response(part: MessagePart) -> MessagePartResponse:
    """转换一个消息内容块。"""
    if isinstance(part, TextPart):
        return TextPartResponse(type="text", text=part.text)
    if isinstance(part, FileRefPart):
        return FileRefPartResponse(
            type="file_ref",
            file_id=part.file_id,
            filename=part.filename,
            mime_type=part.mime_type,
            size_bytes=part.size_bytes,
        )
    if isinstance(part, ToolCallPart):
        return ToolCallPartResponse(
            type="tool_call",
            tool_call_id=part.tool_call_id,
            name=part.name,
            arguments=part.arguments,
        )
    return ToolResultPartResponse(
        type="tool_result",
        tool_call_id=part.tool_call_id,
        status=part.status.value,
        output=part.output,
        artifacts=[
            ArtifactFileRefResponse(
                file_id=artifact.file_id,
                filename=artifact.filename,
                label=artifact.label,
                mime_type=artifact.mime_type,
                size_bytes=artifact.size_bytes,
            )
            for artifact in part.artifacts
        ],
    )


def conversation_message_to_response(
    message: ConversationMessageResult,
) -> ConversationMessageResponse:
    """转换单条会话消息。"""
    return ConversationMessageResponse(
        id=message.id,
        session_id=message.session_id,
        role=message.role.value,
        status=message.status.value,
        sequence=message.sequence,
        parts=[_part_to_response(part) for part in message.parts],
        reply_to_message_id=message.reply_to_message_id,
        run_id=message.run_id,
        created_at=message.created_at,
        completed_at=message.completed_at,
    )


def conversation_page_to_response(
    page: ConversationMessagePageResult,
) -> ConversationMessageListResponse:
    """转换会话消息分页。"""
    return ConversationMessageListResponse(
        items=[conversation_message_to_response(item) for item in page.items],
        next_before_sequence=page.next_before_sequence,
    )


__all__ = ["conversation_message_to_response", "conversation_page_to_response"]
