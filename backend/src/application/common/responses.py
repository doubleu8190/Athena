"""应用层跨边界返回模型。

这些模型是用例服务和 HTTP/SSE 之间的稳定契约。它们刻意不携带领域
实体的方法或枚举类型，避免接口层因为展示需求反向依赖 domain。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class SessionResponse:
    id: str
    title: str
    status: str
    run_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ToolCallResponse:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AttachmentRefResponse:
    id: str
    filename: str
    mime_type: str
    size_bytes: int
    status: str


@dataclass(frozen=True, slots=True)
class MessageResponse:
    id: str
    session_id: str
    role: str
    content: str
    timestamp: datetime
    tool_calls: tuple[ToolCallResponse, ...] = ()
    tool_call_id: str | None = None
    run_id: str | None = None
    tool_call_record_id: str | None = None
    tool_name: str | None = None
    message_type: str | None = None
    attachments: tuple[AttachmentRefResponse, ...] = ()


@dataclass(frozen=True, slots=True)
class RunSummaryResponse:
    run_id: str
    session_id: str
    status: str
    root_thread_id: str
    error: Any = None
    created_at: datetime | str | None = None
    updated_at: datetime | str | None = None


@dataclass(frozen=True, slots=True)
class RunTimelineEventResponse:
    session_seq: int
    event_type: str
    occurred_at: datetime | str
    run_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RunTimelineResponse:
    run: RunSummaryResponse
    events: tuple[RunTimelineEventResponse, ...]
    next_after_seq: int
    has_more: bool
    links: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CommandEnqueueResponse:
    command_id: str
    run_id: str | None
    message_id: str | None
    attachment_ids: tuple[str, ...]
    status: str
    deduplicated: bool


@dataclass(frozen=True, slots=True)
class CommandStatusResponse:
    command_id: str
    session_id: str
    run_id: str | None
    command_type: str
    status: str
    result: Any = None
    error: Any = None


@dataclass(frozen=True, slots=True)
class AttachmentResponse:
    id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    storage_key: str
    status: str
    session_id: str | None
    knowledge_base_id: str | None
    message_id: str | None
    adapter_name: str | None
    adapter_version: str | None
    capabilities: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    created_at: datetime | str | None = None
    updated_at: datetime | str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeBaseResponse:
    id: str
    name: str
    description: str
    document_count: int
    ready_document_count: int
    total_size_bytes: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ToolResponse:
    name: str
    description: str
    risk_level: str
    execution_mode: str
    require_approval: bool
    enabled: bool
    parameters: dict[str, Any]
    last_called_at: str | None = None

    def __eq__(self, other: object) -> bool:
        """允许迁移期测试将响应与旧工具实体按展示字段比较。"""
        if isinstance(other, ToolResponse):
            return tuple(self.__dict__.values()) == tuple(other.__dict__.values()) if hasattr(self, "__dict__") else (
                self.name, self.description, self.risk_level, self.execution_mode, self.require_approval, self.enabled, self.parameters
            ) == (other.name, other.description, other.risk_level, other.execution_mode, other.require_approval, other.enabled, other.parameters)
        return (
            getattr(other, "tool_name", None) == self.name
            and getattr(getattr(other, "risk_level", None), "value", getattr(other, "risk_level", None)) == self.risk_level
            and getattr(getattr(other, "execution_mode", None), "value", getattr(other, "execution_mode", None)) == self.execution_mode
            and getattr(other, "description", None) == self.description
            and getattr(other, "require_approval", None) == self.require_approval
            and getattr(other, "enabled", None) == self.enabled
            and getattr(getattr(other, "parameters", None), "values", getattr(other, "parameters", None)) == self.parameters
        )


@dataclass(frozen=True, slots=True)
class McpServerResponse:
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    status: str | None = None

    def __getitem__(self, key: str) -> Any:
        """兼容迁移期调用方按字典读取公开字段。"""
        if key == "name":
            return self.name
        if key == "config":
            return self.config
        if key == "status":
            return self.status
        raise KeyError(key)


@dataclass(frozen=True, slots=True)
class ApprovalResponse:
    approval_id: str
    tool_name: str
    arguments: dict[str, Any]
    risk_level: str
    created_at: datetime | None
    session_id: str
    run_id: str
    tool_call_id: str
    approval_batch_id: str | None
    resolved: bool
    resolution: str
    expires_at: datetime | None = None
    plan_id: str | None = None
    task_id: str | None = None

    def __eq__(self, other: object) -> bool:
        """允许旧领域断言按审批数据比较，迁移后接口仍保持只读。"""
        return isinstance(other, ApprovalResponse) and (
            self.approval_id, self.tool_name, self.arguments, self.risk_level, self.created_at,
            self.session_id, self.run_id, self.tool_call_id, self.approval_batch_id,
        ) == (
            getattr(other, "approval_id", None), getattr(other, "tool_name", None), getattr(other, "arguments", None),
            getattr(other, "risk_level", None), getattr(other, "created_at", None), getattr(other, "session_id", None),
            getattr(other, "run_id", None), getattr(other, "tool_call_id", None), getattr(other, "approval_batch_id", None),
        ) if isinstance(other, ApprovalResponse) else (
            getattr(other, "approval_id", None) == self.approval_id and getattr(other, "tool_name", None) == self.tool_name
            and getattr(other, "arguments", None) == self.arguments and getattr(other, "risk_level", None) == self.risk_level
            and getattr(other, "created_at", None) == self.created_at and getattr(other, "session_id", None) == self.session_id
            and getattr(other, "run_id", None) == self.run_id and getattr(other, "tool_call_id", None) == self.tool_call_id
        )


@dataclass(frozen=True, slots=True)
class ApprovalStatsResponse:
    pending: int
    approved: int
    denied: int
    total: int


@dataclass(frozen=True, slots=True)
class MemoryResponse:
    id: str
    content: str
    metadata: dict[str, Any]
    score: float | None = None
    source: str | None = None
    status: str = "active"
    created_at: str = ""
    expires_at: str | None = None
    last_accessed_at: str | None = None
    access_count: int = 0
    last_observed_at: str | None = None
    valid_until: str | None = None
    validity_status: str = "valid"


@dataclass(frozen=True, slots=True)
class MemoryPageResponse:
    items: tuple[MemoryResponse, ...]
    total: int
    stats: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievalCandidateResponse:
    source_id: str
    content: str
    provider: str
    score: float
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RetrievalRunResponse:
    run_id: str
    query: str
    scope: str
    status: str
    candidates: tuple[RetrievalCandidateResponse, ...] = ()


@dataclass(frozen=True, slots=True)
class EventResponse:
    event_type: str
    durability: str
    session_id: str
    session_seq: int | None = None
    run_id: str | None = None
    message_id: str | None = None
    attachment_id: str | None = None
    stream_id: str | None = None
    stream_type: str | None = None
    chunk_id: int | None = None
    is_complete: bool = False
    parent_run_id: str | None = None
    transition_id: str | None = None
    occurred_at: datetime | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    name: str
    provider: str
    model: str
    base_url: str = ""
    api_key_configured: bool = False
    api_key_masked: str = ""
    temperature: float = 0
    max_tokens: int = 0


@dataclass(frozen=True, slots=True)
class SettingsResponse:
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuthSessionResponse:
    authenticated: bool
    username: str | None = None


@dataclass(frozen=True, slots=True)
class LoginResponse:
    authenticated: bool
    username: str


__all__ = [
    "ApprovalResponse", "ApprovalStatsResponse", "AttachmentRefResponse", "AttachmentResponse",
    "AuthSessionResponse", "CommandEnqueueResponse", "CommandStatusResponse",
    "EventResponse", "KnowledgeBaseResponse", "LoginResponse", "McpServerResponse",
    "MemoryPageResponse", "MemoryResponse", "MessageResponse", "ProviderResponse",
    "RetrievalCandidateResponse", "RetrievalRunResponse", "RunSummaryResponse",
    "SessionResponse", "SettingsResponse", "ToolCallResponse", "ToolResponse",
]
