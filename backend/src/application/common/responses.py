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
    """会话标识、项目归属和会话元数据的响应模型。
    """
    id: str
    title: str
    active_run_id: str | None
    created_at: datetime
    updated_at: datetime
    project_id: str | None = None
    last_activity_at: datetime | None = None
    idempotency_replayed: bool = False


@dataclass(frozen=True, slots=True)
class ToolCallResponse:
    """对话消息中工具调用名称、参数和标识的响应模型。
    """
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AttachmentRefResponse:
    """消息引用的附件标识及简要元数据。
    """
    id: str
    filename: str
    mime_type: str
    size_bytes: int
    status: str


@dataclass(frozen=True, slots=True)
class MessageResponse:
    """会话消息正文、角色、时间和附件引用。
    """
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
    """Run 状态、摘要、结果和时间信息的响应模型。
    """
    run_id: str
    session_id: str
    message_id: str
    sequence: int
    status: str
    root_thread_id: str
    error: Any = None
    created_at: datetime | str | None = None
    updated_at: datetime | str | None = None
    started_at: datetime | str | None = None
    title: str = ""


@dataclass(frozen=True, slots=True)
class RunListResponse:
    """Run 摘要列表、总数和分页状态。
    """
    items: tuple[RunSummaryResponse, ...]
    total: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class RunTimelineEventResponse:
    """Run 时间线中的单条事件及其关联字段。
    """
    session_seq: int
    event_type: str
    occurred_at: datetime | str
    run_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RunTimelineResponse:
    """Run 摘要、分页事件和关联资源标识。
    """
    run: RunSummaryResponse
    events: tuple[RunTimelineEventResponse, ...]
    next_after_seq: int
    has_more: bool
    links: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunEnqueueResponse:
    """Run 提交成功后的标识和队列状态。
    """
    session_id: str
    run_id: str
    message_id: str
    sequence: int
    status: str
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class RunStopResponse:
    """Run 停止请求的状态及幂等处理结果。
    """
    run_id: str | None
    status: str
    no_op: bool = False
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class AttachmentResponse:
    """附件标识、文件元数据和处理状态。
    """
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
    """知识库标识、名称、说明和文档统计。
    """
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
    """工具配置、能力和治理策略。
    """
    name: str
    description: str
    risk_level: str
    execution_mode: str
    require_approval: bool
    enabled: bool
    parameters: dict[str, Any]
    last_called_at: str | None = None

    def __eq__(self, other: object) -> bool:
        """允许测试将响应与工具实体按展示字段比较。

        参数：
        - `other`（object）：用于与当前值比较的对象；约束：无额外运行时限制。

        返回：
        - bool：操作是否成功或条件是否成立。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
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
    """MCP 服务配置的可公开字段和运行状态。
    """
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    status: str | None = None

    def __getitem__(self, key: str) -> Any:
        """允许调用方按字典方式读取公开字段。

        参数：
        - `key`（str）：当前字段或映射项的键；约束：无额外运行时限制。

        返回：
        - Any：按字段名返回名称、配置或状态中的一个公开字段。

        异常：
        - KeyError：执行到该异常分支时。
        - 未列出的依赖异常按当前实现传播。
        """
        if key == "name":
            return self.name
        if key == "config":
            return self.config
        if key == "status":
            return self.status
        raise KeyError(key)


@dataclass(frozen=True, slots=True)
class ApprovalResponse:
    """审批请求身份、目标操作和当前决定。
    """
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

@dataclass(frozen=True, slots=True)
class ApprovalStatsResponse:
    """审批状态分类统计和总数。
    """
    pending: int
    approved: int
    denied: int
    total: int


@dataclass(frozen=True, slots=True)
class MemoryResponse:
    """记忆内容、作用域、有效状态和来源摘要。
    """
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
    """记忆分页条目、总数和分类统计。
    """
    items: tuple[MemoryResponse, ...]
    total: int
    stats: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievalCandidateResponse:
    """检索候选的来源、内容、分数和筛选状态。
    """
    candidate_id: str
    run_id: str
    provider: str
    stage: str
    source_type: str
    source_id: str
    logical_source_id: str | None = None
    revision_id: str | None = None
    native_rank: int | None = None
    native_score: float | None = None
    fused_rank: int | None = None
    fused_score: float | None = None
    rerank_rank: int | None = None
    rerank_score: float | None = None
    selected_for_result: bool = False
    filter_reason: str | None = None
    content_preview: str | None = None
    source_title: str | None = None
    locator: dict[str, Any] = field(default_factory=dict)
    hits: tuple[dict[str, Any], ...] = ()
    context_selected: bool | None = None
    selection_stage: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class RetrievalRunResponse:
    """检索运行状态、Provider 结果和候选追踪。
    """
    run_id: str
    query: str
    scope: str
    status: str
    session_id: str | None = None
    agent_run_id: str | None = None
    message_id: str | None = None
    trace_status: str = "incomplete"
    raw_hit_count: int | None = None
    candidate_count: int = 0
    selected_count: int = 0
    context_count: int | None = None
    selection_status: str = "incomplete"
    providers: tuple[dict[str, Any], ...] = ()
    config: dict[str, Any] = field(default_factory=dict)
    index_generation: str | None = None
    duration_ms: float | None = None
    error_message: str | None = None
    created_at: str = ""
    completed_at: str | None = None
    candidates: tuple[RetrievalCandidateResponse, ...] = ()


@dataclass(frozen=True, slots=True)
class EventResponse:
    """会话事件序号、类型、载荷和发生时间。
    """
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
    """Provider 名称、启用状态和配置摘要。
    """
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
    """当前应用可公开的设置值。
    """
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuthSessionResponse:
    """会话认证状态和已验证用户名。
    """
    authenticated: bool
    username: str | None = None


@dataclass(frozen=True, slots=True)
class LoginResponse:
    """登录凭据校验后的认证结果。
    """
    authenticated: bool
    username: str


__all__ = [
    "ApprovalResponse", "ApprovalStatsResponse", "AttachmentRefResponse", "AttachmentResponse",
    "AuthSessionResponse",
    "EventResponse", "KnowledgeBaseResponse", "LoginResponse", "McpServerResponse",
    "MemoryPageResponse", "MemoryResponse", "MessageResponse", "ProviderResponse",
    "RetrievalCandidateResponse", "RetrievalRunResponse", "RunEnqueueResponse", "RunStopResponse", "RunSummaryResponse", "RunListResponse",
    "SessionResponse", "SettingsResponse", "ToolCallResponse", "ToolResponse",
]
