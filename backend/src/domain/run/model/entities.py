"""Run 的状态、事件和原子命令契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from domain.memory.lifecycle import (
    ExtractionStatus,
    RunMemoryContextState,
)
from domain.message import ConversationMessage, ConversationMessageDraft, ExecutionMessageDraft
from domain.skill import RunSkillSnapshot
from domain.tool import (
    RuntimeToolSnapshot,
    ToolApprovalDecision,
    ToolCallStatus,
)


class RunStatus(StrEnum):
    """Run 生命周期状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ContextStopReason(StrEnum):
    """上下文超过安全线时允许向客户端公开的稳定原因。"""

    NO_COMPRESSIBLE_HISTORY = "no_compressible_history"
    COMPRESSION_NOT_CONFIGURED = "compression_not_configured"
    COMPRESSION_MODEL_UNAVAILABLE = "compression_model_unavailable"
    COMPRESSION_BUDGET_EXHAUSTED = "compression_budget_exhausted"
    COMPRESSION_INPUT_TOO_LARGE = "compression_input_too_large"
    COMPRESSION_FAILED = "compression_failed"
    COMPRESSION_INSUFFICIENT = "compression_insufficient"
    HARD_WINDOW_EXCEEDED = "hard_window_exceeded"


class InputRequestStatus(StrEnum):
    """输入请求状态。"""

    PENDING = "pending"
    ANSWERED = "answered"
    CLOSED = "closed"


class RunRequestOperation(StrEnum):
    """客户端命令幂等操作范围。"""

    SUBMIT = "submit"
    ANSWER = "answer"
    RETRY = "retry"
    DECIDE_TOOL_APPROVAL = "decide_tool_approval"


class RunAttemptUsage(BaseModel):
    """按执行代次和调用序号去重的单次已知用量。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_epoch: int = Field(ge=1)
    attempt: int = Field(ge=1)
    purpose: Literal["answer", "compression"] = "answer"
    operation_id: str | None = None
    batch_index: int = Field(default=0, ge=0)
    model_profile_id: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    complete: bool = False


class RunUsage(BaseModel):
    """已知模型用量及其完整性。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    complete: bool = False
    attempts: tuple[RunAttemptUsage, ...] = ()
    answer_tokens: int | None = Field(default=None, ge=0)
    compression_tokens: int | None = Field(default=None, ge=0)
    legacy_input_tokens: int | None = Field(default=None, ge=0)
    legacy_output_tokens: int | None = Field(default=None, ge=0)
    legacy_total_tokens: int | None = Field(default=None, ge=0)
    legacy_answer_tokens: int | None = Field(default=None, ge=0)
    legacy_complete: bool = False


class RunInputFiles(BaseModel):
    """Run 创建与澄清过程中由用户明确关联的会话文件集合。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    file_ids: tuple[str, ...] = ()

    def model_post_init(self, __context: object) -> None:
        if len(self.file_ids) != len(set(self.file_ids)):
            raise ValueError("run_input_file_ids_duplicate")

    @property
    def ids(self) -> list[str]:
        """返回持久化 JSON 使用的稳定列表。"""
        return list(self.file_ids)


@dataclass(frozen=True, slots=True)
class RunInputFileContext:
    """经鉴权后供模型引用的最小文件元数据。"""

    file_id: str
    filename: str
    label: str | None
    mime_type: str
    size_bytes: int
    source: str


@dataclass(frozen=True, slots=True)
class RecordRunUsageCommand:
    """在有效租约下记录一次幂等模型调用用量。"""

    run_id: str
    worker_id: str
    execution_epoch: int
    attempt: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    complete: bool
    recorded_at: datetime
    purpose: Literal["answer", "compression"] = "answer"
    operation_id: str | None = None
    batch_index: int = 0
    model_profile_id: str | None = None
    duration_ms: int | None = None


class _RunEventPayload(BaseModel):
    """状态事件公共 JSON 配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RunQueuedPayload(_RunEventPayload):
    type: Literal["queued"] = "queued"
    trigger_message_id: str


class RunStartedPayload(_RunEventPayload):
    type: Literal["started"] = "started"
    execution_epoch: int
    worker_id: str


class RunWaitingPayload(_RunEventPayload):
    type: Literal["waiting_for_input"] = "waiting_for_input"
    input_request_id: str
    question_message_id: str


class RunResumedPayload(_RunEventPayload):
    type: Literal["resumed"] = "resumed"
    input_request_id: str
    answer_message_id: str


class RunWaitingForApprovalPayload(_RunEventPayload):
    type: Literal["waiting_for_approval"] = "waiting_for_approval"
    batch_message_id: str
    pending_call_ids: tuple[str, ...]


class RunToolApprovalDecidedPayload(_RunEventPayload):
    type: Literal["tool_approval_decided"] = "tool_approval_decided"
    call_id: str
    ordinal: int = Field(ge=1)
    decision: ToolApprovalDecision
    status: ToolCallStatus


class RunApprovalResumedPayload(_RunEventPayload):
    type: Literal["approval_resumed"] = "approval_resumed"
    batch_message_id: str


class RunCancelRequestedPayload(_RunEventPayload):
    type: Literal["cancel_requested"] = "cancel_requested"


class RunCompletedPayload(_RunEventPayload):
    type: Literal["completed"] = "completed"
    assistant_message_id: str


class RunFailedPayload(_RunEventPayload):
    type: Literal["failed"] = "failed"
    safe_error_code: str
    report_message_id: str | None = None
    context_stop_reason: ContextStopReason | None = None


class RunCancelledPayload(_RunEventPayload):
    type: Literal["cancelled"] = "cancelled"


class RunCompressionStartedPayload(_RunEventPayload):
    type: Literal["compression_started"] = "compression_started"
    operation_id: str


class RunCompressionCompletedPayload(_RunEventPayload):
    type: Literal["compression_completed"] = "compression_completed"
    operation_id: str
    summary_id: str
    covered_through_sequence: int
    batch_count: int


class RunCompressionFailedPayload(_RunEventPayload):
    type: Literal["compression_failed"] = "compression_failed"
    operation_id: str
    safe_error_code: str
    batch_count: int
    candidate_base_summary_id: str | None = None
    candidate_covered_through_sequence: int | None = None


class RunToolCallStartedPayload(_RunEventPayload):
    type: Literal["tool_call_started"] = "tool_call_started"
    call_id: str
    ordinal: int = Field(ge=1)
    round: int = Field(default=0, ge=0)


class RunToolCallFinishedPayload(_RunEventPayload):
    type: Literal["tool_call_finished"] = "tool_call_finished"
    call_id: str
    ordinal: int = Field(ge=1)
    status: ToolCallStatus
    round: int = Field(default=0, ge=0)
    safe_error_code: str | None = None


type RunEventPayload = Annotated[
    RunQueuedPayload
    | RunStartedPayload
    | RunWaitingPayload
    | RunResumedPayload
    | RunWaitingForApprovalPayload
    | RunToolApprovalDecidedPayload
    | RunApprovalResumedPayload
    | RunCancelRequestedPayload
    | RunCompletedPayload
    | RunFailedPayload
    | RunCancelledPayload
    | RunCompressionStartedPayload
    | RunCompressionCompletedPayload
    | RunCompressionFailedPayload
    | RunToolCallStartedPayload
    | RunToolCallFinishedPayload,
    Field(discriminator="type"),
]


class RunRequestResult(BaseModel):
    """命令幂等记录返回的资源引用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str | None = None
    message_id: str | None = None
    input_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class Run:
    """Run 的持久化领域投影。"""

    id: str
    session_id: str
    owner_id: str
    status: RunStatus
    trigger_message_id: str
    context_cutoff_sequence: int
    retry_of_run_id: str | None
    model_config_version_id: str
    main_prompt_version_id: str
    compression_prompt_version_id: str
    prompt_template_version: str
    event_sequence: int
    execution_epoch: int
    worker_id: str | None
    lease_expires_at: datetime | None
    execution_started_at: datetime | None
    execution_deadline_at: datetime | None
    queued_at: datetime
    first_started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
    cancel_requested_at: datetime | None
    safe_error_code: str | None
    usage: RunUsage
    compression_policy_version_id: str | None = None
    tool_snapshot: RuntimeToolSnapshot | None = None
    auth_session_id: str | None = None
    llm_request_count: int = 0
    skill_snapshot: RunSkillSnapshot | None = None
    input_files: RunInputFiles = RunInputFiles()
    report_message_id: str | None = None
    memory_project_id: str | None = None
    memory_input_message_ids: tuple[str, ...] = ()
    memory_context_state: RunMemoryContextState = RunMemoryContextState()
    memory_extraction_status: ExtractionStatus | None = None
    memory_extracted_at: datetime | None = None
    memory_extraction_batch_id: str | None = None
    memory_extraction_safe_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class InitializeRunMemoryContextCommand:
    """当前执行代次首次固定 Run 可读取的记忆引用。"""

    run_id: str
    worker_id: str
    execution_epoch: int
    context_state: RunMemoryContextState


@dataclass(frozen=True, slots=True)
class RunInputRequest:
    """等待用户回答的单次输入请求。"""

    id: str
    run_id: str
    session_id: str
    question_message_id: str
    status: InputRequestStatus
    answer_message_id: str | None
    created_at: datetime
    answered_at: datetime | None
    closed_at: datetime | None


@dataclass(frozen=True, slots=True)
class RunInputCall:
    """澄清请求对应的协议调用引用。"""

    tool_call_id: str
    thread_id: str
    question_message_id: str


@dataclass(frozen=True, slots=True)
class RunEvent:
    """按 Run 内序号排列的状态事件。"""

    id: str
    run_id: str
    sequence: int
    type: str
    created_at: datetime
    payload: RunEventPayload


@dataclass(frozen=True, slots=True)
class RunRequest:
    """客户端命令的幂等账本记录。"""

    owner_id: str
    session_id: str
    operation: RunRequestOperation
    request_id: str
    fingerprint: str
    result: RunRequestResult
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SubmitRunCommand:
    """原子提交文本任务所需的数据。"""

    run_id: str
    request_id: str
    fingerprint: str
    prompt_template_version: str
    message: ConversationMessageDraft
    queued_at: datetime
    selected_skill_ids: tuple[str, ...] = ()
    input_files: RunInputFiles = RunInputFiles()
    auth_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class SubmitRunResult:
    """任务提交结果，可用于幂等重放。"""

    run: Run
    message: ConversationMessage
    replayed: bool


@dataclass(frozen=True, slots=True)
class AnswerRunCommand:
    """原子回答输入请求并恢复 Run 所需的数据。"""

    run_id: str
    input_request_id: str
    request_id: str
    fingerprint: str
    answer_message: ConversationMessageDraft
    execution_message: ExecutionMessageDraft
    queued_at: datetime
    input_files: RunInputFiles = RunInputFiles()
    auth_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class RequestRunInputCommand:
    """原子等待用户输入所需的助手消息与协议交互。"""

    run_id: str
    worker_id: str
    execution_epoch: int
    input_request_id: str
    question_message: ConversationMessageDraft
    execution_messages: tuple[ExecutionMessageDraft, ...]
    usage: RunUsage
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FinishRunCommand:
    """原子保存最终助手回复和已知用量。"""

    run_id: str
    worker_id: str
    execution_epoch: int
    reply_message: ConversationMessageDraft
    usage: RunUsage
    finished_at: datetime


@dataclass(frozen=True, slots=True)
class FailRunCommand:
    """原子记录一次安全失败。"""

    run_id: str
    worker_id: str
    execution_epoch: int
    safe_error_code: str
    failed_at: datetime
    report_message: ConversationMessageDraft | None = None
    usage: RunUsage = field(default_factory=RunUsage)
    context_stop_reason: ContextStopReason | None = None


@dataclass(frozen=True, slots=True)
class RunLease:
    """Worker 领取 Run 时取得的租约快照。"""

    run: Run
    lease_expires_at: datetime
    execution_deadline_at: datetime


__all__ = [
    "AnswerRunCommand",
    "ContextStopReason",
    "InputRequestStatus",
    "InitializeRunMemoryContextCommand",
    "FailRunCommand",
    "FinishRunCommand",
    "Run",
    "RunAttemptUsage",
    "RunCancelledPayload",
    "RunCancelRequestedPayload",
    "RunCompletedPayload",
    "RunEvent",
    "RunEventPayload",
    "RunFailedPayload",
    "RunInputRequest",
    "RunInputFiles",
    "RunInputCall",
    "RunLease",
    "RunQueuedPayload",
    "RecordRunUsageCommand",
    "RunRequest",
    "RunRequestOperation",
    "RunRequestResult",
    "RequestRunInputCommand",
    "RunResumedPayload",
    "RunStartedPayload",
    "RunStatus",
    "RunToolCallFinishedPayload",
    "RunToolCallStartedPayload",
    "RunUsage",
    "RunWaitingPayload",
    "SubmitRunCommand",
    "SubmitRunResult",
]
