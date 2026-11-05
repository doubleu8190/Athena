"""消息实体、写入草稿与分页结果。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from domain.message.model.part import MessagePart


class ConversationRole(StrEnum):
    """会话流的参与者角色。"""

    USER = "user"
    ASSISTANT = "assistant"


class ExecutionRole(StrEnum):
    """执行轨迹的模型消息角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class MessageStatus(StrEnum):
    """会话消息的生命周期状态。"""

    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class MessageMetadata:
    """允许保存的内部消息元数据白名单。"""

    provider: str | None = None
    model: str | None = None
    finish_reason: str | None = None
    trace_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationMessageDraft:
    """尚未分配会话内顺序的会话消息。"""

    id: str
    session_id: str
    owner_id: str
    role: ConversationRole
    status: MessageStatus
    parts: tuple[MessagePart, ...]
    reply_to_message_id: str | None
    run_id: str | None
    client_message_id: str | None
    created_at: datetime
    completed_at: datetime | None
    metadata: MessageMetadata


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """已分配会话内顺序的会话消息。"""

    id: str
    session_id: str
    role: ConversationRole
    status: MessageStatus
    sequence: int
    parts: tuple[MessagePart, ...]
    reply_to_message_id: str | None
    run_id: str | None
    client_message_id: str | None
    created_at: datetime
    completed_at: datetime | None
    metadata: MessageMetadata


@dataclass(frozen=True, slots=True)
class ExecutionMessageDraft:
    """尚未分配会话内顺序的执行轨迹消息。"""

    id: str
    session_id: str
    owner_id: str
    run_id: str
    thread_id: str
    role: ExecutionRole
    parts: tuple[MessagePart, ...]
    tool_call_id: str | None
    source_message_id: str | None
    created_at: datetime
    metadata: MessageMetadata


@dataclass(frozen=True, slots=True)
class ExecutionMessage:
    """已分配会话内顺序的执行轨迹消息。"""

    id: str
    session_id: str
    run_id: str
    thread_id: str
    sequence: int
    role: ExecutionRole
    parts: tuple[MessagePart, ...]
    tool_call_id: str | None
    source_message_id: str | None
    created_at: datetime
    metadata: MessageMetadata


@dataclass(frozen=True, slots=True)
class ConversationMessagePage:
    """按 sequence 升序排列的一页会话消息。"""

    items: tuple[ConversationMessage, ...]
    next_before_sequence: int | None


__all__ = [
    "ConversationMessage",
    "ConversationMessageDraft",
    "ConversationMessagePage",
    "ConversationRole",
    "ExecutionMessage",
    "ExecutionMessageDraft",
    "ExecutionRole",
    "MessageMetadata",
    "MessageStatus",
]
