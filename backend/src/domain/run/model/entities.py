"""Run 的状态、事件和原子命令契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from domain.message import ConversationMessage, ConversationMessageDraft, ExecutionMessageDraft
from domain.tool import RuntimeToolSnapshot, ToolCallStatus


class RunStatus(StrEnum):
    """Run 生命周期状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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


class RunCancelRequestedPayload(_RunEventPayload):
    type: Literal["cancel_requested"] = "cancel_requested"


class RunCompletedPayload(_RunEventPayload):
    type: Literal["completed"] = "completed"
    assistant_message_id: str


class RunFailedPayload(_RunEventPayload):
    type: Literal["failed"] = "failed"
    safe_error_code: str


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


type RunEventPayload = Annotated[
    RunQueuedPayload
    | RunStartedPayload
    | RunWaitingPayload
    | RunResumedPayload
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


@dataclass(frozen=True, slots=True)
class RunLease:
    """Worker 领取 Run 时取得的租约快照。"""

    run: Run
    lease_expires_at: datetime
    execution_deadline_at: datetime


__all__ = [
    "AnswerRunCommand",
    "InputRequestStatus",
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
