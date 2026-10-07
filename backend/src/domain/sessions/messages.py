"""会话消息及其嵌套值对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from domain.files.entities import AttachmentStatus


class MessageRole(StrEnum):
    """消息在会话中的参与者角色。"""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """消息中记录的标准化工具调用。"""

    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    """消息使用的附件轻量引用，不包含存储层密钥。"""

    id: str
    filename: str
    mime_type: str
    size_bytes: int
    status: AttachmentStatus


@dataclass(frozen=True, slots=True)
class Message:
    """与 Web、ORM 和 Pydantic 无关的会话消息实体。"""

    id: str
    session_id: str
    role: MessageRole
    content: str
    timestamp: datetime
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    run_id: str | None = None
    tool_call_record_id: str | None = None
    tool_name: str | None = None
    message_type: str | None = None
    attachments: tuple[AttachmentRef, ...] = ()
