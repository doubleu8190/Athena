"""消息 HTTP 响应 DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class TextPartResponse(BaseModel):
    """文本内容块。"""

    type: Literal["text"] = "text"
    text: str


class FileRefPartResponse(BaseModel):
    """文件引用内容块。"""

    type: Literal["file_ref"] = "file_ref"
    file_id: str
    filename: str
    mime_type: str | None = None
    size_bytes: int | None = None


class ToolCallPartResponse(BaseModel):
    """工具调用内容块。"""

    type: Literal["tool_call"] = "tool_call"
    tool_call_id: str
    name: str
    arguments: dict[str, object]


class ToolResultPartResponse(BaseModel):
    """工具结果内容块。"""

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    status: str
    output: str | None = None
    artifact_id: str | None = None


type MessagePartResponse = Annotated[
    TextPartResponse | FileRefPartResponse | ToolCallPartResponse | ToolResultPartResponse,
    Field(discriminator="type"),
]


class ConversationMessageResponse(BaseModel):
    """一条公开会话消息。"""

    id: str
    session_id: str
    role: str
    status: str
    sequence: int
    parts: list[MessagePartResponse]
    reply_to_message_id: str | None
    run_id: str | None
    created_at: datetime
    completed_at: datetime | None


class ConversationMessageListResponse(BaseModel):
    """会话消息分页响应。"""

    items: list[ConversationMessageResponse]
    next_before_sequence: int | None


__all__ = [
    "ConversationMessageListResponse",
    "ConversationMessageResponse",
    "FileRefPartResponse",
    "MessagePartResponse",
    "TextPartResponse",
    "ToolCallPartResponse",
    "ToolResultPartResponse",
]
