"""Trusted execution context, atomic commands, and protocol-safe outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from common.json import dumps
from domain.file import PendingSessionFile
from domain.message import (
    ArtifactFileRef,
    ConversationMessageDraft,
    ExecutionMessageDraft,
    ToolResultStatus,
)
from domain.skill import (
    RunSkillSnapshot,
    SafeSkillApprovalProjection,
    SkillExecutionCompletion,
)

if TYPE_CHECKING:
    from domain.tool.model.runtime import RuntimeToolSnapshot

MAX_TOOL_ARGUMENT_BYTES = 64 * 1024
MAX_TOOL_RESULT_BYTES = 64 * 1024
MAX_TOOL_CALL_ID_LENGTH = 256
MAX_LLM_REQUESTS_PER_RUN = 40


class ToolKind(StrEnum):
    """Runtime dispatch source."""

    BUILTIN = "builtin"
    MCP = "mcp"


class ToolCallStatus(StrEnum):
    """Persisted lifecycle of a model-issued tool call."""

    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ToolApprovalDecision(StrEnum):
    """A user's one-time decision for a persisted tool call."""

    APPROVE = "approve"
    REJECT = "reject"


class ToolOutcomeStatus(StrEnum):
    """Business outcome independent of the provider-visible result role."""

    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ToolOutcome(BaseModel):
    """Bounded result envelope safe to persist and return to the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ToolOutcomeStatus
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(default="", max_length=2000)
    data: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False
    cleanup_error_code: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _validate_json(self) -> ToolOutcome:
        try:
            dumps(self.model_dump(mode="json"))
        except (TypeError, ValueError, UnicodeError) as error:
            raise ValueError("tool_outcome_json_invalid") from error
        return self

    def canonical_output(self) -> str:
        """Serialize the stable envelope stored in ToolResultPart.output."""
        return dumps(self.model_dump(mode="json"))

    @classmethod
    def from_output(cls, value: str) -> ToolOutcome:
        """Restore an outcome from a previously persisted envelope."""
        return cls.model_validate_json(value)


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Server-owned identity and deadline; model arguments cannot override it."""

    owner_id: str
    chat_session_id: str
    project_id: str | None
    run_id: str
    execution_epoch: int
    auth_session_id: str
    deadline_at: datetime
    tool_call_record_id: str | None = None
    skill_snapshot: RunSkillSnapshot | None = None


@dataclass(frozen=True, slots=True)
class AuthorizedRunContext:
    """Fresh authentication and visible chat project snapshot."""

    owner_id: str
    chat_session_id: str
    project_id: str | None
    auth_session_id: str


@dataclass(frozen=True, slots=True)
class RunToolCallRecord:
    """Persisted execution record; arguments/results live in messages."""

    id: str
    run_id: str
    session_id: str
    tool_call_id: str
    ordinal: int
    tool_id: str | None
    tool_kind: ToolKind | None
    model_name: str
    execution_epoch: int
    status: ToolCallStatus
    call_message_id: str
    result_message_id: str | None
    started_at: datetime | None
    finished_at: datetime | None
    safe_error_code: str | None
    outcome: ToolOutcome | None
    requires_approval: bool = False
    requested_at: datetime | None = None
    approval_decided_at: datetime | None = None
    approval_decision: ToolApprovalDecision | None = None
    safe_skill_projection: SafeSkillApprovalProjection | None = None


@dataclass(frozen=True, slots=True)
class RunToolCallDetails:
    """Authorized detail projection with bounded protocol payloads."""

    record: RunToolCallRecord
    arguments: dict[str, Any]
    output: str | None
    artifacts: tuple[ArtifactFileRef, ...] = ()
    safe_skill_projection: SafeSkillApprovalProjection | None = None


@dataclass(frozen=True, slots=True)
class RunToolCallPage:
    """One ordinal-ordered page of call records."""

    items: tuple[RunToolCallRecord, ...]
    next_after_ordinal: int | None


@dataclass(frozen=True, slots=True)
class InitializeRunToolsCommand:
    """Freeze a validated runtime catalog under a live execution lease."""

    run_id: str
    worker_id: str
    execution_epoch: int
    snapshot: RuntimeToolSnapshot


@dataclass(frozen=True, slots=True)
class ReserveLlmRequestCommand:
    """Atomically consume one Run-level model request slot."""

    run_id: str
    worker_id: str
    execution_epoch: int
    maximum_count: int = MAX_LLM_REQUESTS_PER_RUN


@dataclass(frozen=True, slots=True)
class FinishToolCallCommand:
    """Persist one idempotent result, optionally terminating the whole Run."""

    run_id: str
    worker_id: str
    execution_epoch: int
    record_id: str
    result_message: ExecutionMessageDraft
    outcome: ToolOutcome
    finished_at: datetime
    artifacts: tuple[PendingSessionFile, ...] = ()
    delivery_message: ConversationMessageDraft | None = None
    skill_execution_completion: SkillExecutionCompletion | None = None
    terminate_error_code: str | None = None
    defer_terminal_convergence: bool = False


@dataclass(frozen=True, slots=True)
class BeginToolCallBatchItem:
    """One call in an atomic assistant tool-message batch."""

    record_id: str
    tool_call_id: str
    model_name: str
    tool_id: str | None
    tool_kind: ToolKind | None
    requires_approval: bool
    immediate_outcome: ToolOutcome | None = None
    result_message: ExecutionMessageDraft | None = None
    safe_skill_projection: SafeSkillApprovalProjection | None = None


@dataclass(frozen=True, slots=True)
class BeginToolCallBatchCommand:
    """Persist one assistant response and all its call records atomically."""

    run_id: str
    worker_id: str
    execution_epoch: int
    call_message: ExecutionMessageDraft
    calls: tuple[BeginToolCallBatchItem, ...]
    requested_at: datetime
    round: int = 0


@dataclass(frozen=True, slots=True)
class RunToolBatchCall:
    """A persisted call paired with its frozen assistant arguments."""

    record: RunToolCallRecord
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RunToolBatch:
    """One persisted assistant tool batch in stable ordinal order."""

    call_message_id: str
    thread_id: str
    round: int
    calls: tuple[RunToolBatchCall, ...]


@dataclass(frozen=True, slots=True)
class BeginToolCallBatchResult:
    """The committed batch and whether the Run entered approval waiting."""

    batch: RunToolBatch
    waiting_for_approval: bool
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class AdmitToolCallCommand:
    """Atomically move one approved call into real execution."""

    run_id: str
    worker_id: str
    execution_epoch: int
    record_id: str
    admitted_at: datetime


@dataclass(frozen=True, slots=True)
class ConvergeToolBatchCommand:
    """Cancel undispatched calls and fail a Run after batch settlement."""

    run_id: str
    worker_id: str
    execution_epoch: int
    call_message_id: str
    safe_error_code: str
    failed_at: datetime


@dataclass(frozen=True, slots=True)
class DecideToolApprovalCommand:
    """User decision command with its Run-request idempotency ledger key."""

    session_id: str
    owner_id: str
    run_id: str
    record_id: str
    request_id: str
    fingerprint: str
    decision: ToolApprovalDecision
    decided_at: datetime
    auth_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class PendingToolApproval:
    """Safe Run-detail projection for one call awaiting a decision."""

    call_id: str
    batch_message_id: str
    ordinal: int
    model_name: str
    tool_kind: ToolKind | None
    requested_at: datetime
    safe_skill_projection: SafeSkillApprovalProjection | None = None


@dataclass(frozen=True, slots=True)
class ToolCallPreparation:
    """本地校验和 Run 固定策略解析后的单次调用准备结果。"""

    outcome: ToolOutcome | None
    requires_approval: bool
    safe_skill_projection: SafeSkillApprovalProjection | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """工具结果及同事务待发布的文件/脚本附加信息。"""

    outcome: ToolOutcome
    terminate_run_error_code: str | None = None
    artifacts: tuple[PendingSessionFile, ...] = ()
    skill_execution_completion: SkillExecutionCompletion | None = None


@dataclass(frozen=True, slots=True)
class ToolRetryContext:
    """Safe source-Run context for a user-confirmed whole-task retry."""

    run_id: str
    tool_name: str
    safe_error_code: str


def persisted_status_for_outcome(outcome: ToolOutcome) -> ToolCallStatus:
    """Map a business outcome to its persisted lifecycle state."""
    if outcome.status is ToolOutcomeStatus.OK:
        return ToolCallStatus.COMPLETED
    if outcome.status is ToolOutcomeStatus.ERROR:
        return ToolCallStatus.FAILED
    if outcome.status is ToolOutcomeStatus.CANCELLED:
        return ToolCallStatus.CANCELLED
    return ToolCallStatus.UNKNOWN


def protocol_status_for_outcome(outcome: ToolOutcome) -> ToolResultStatus:
    """Only a confirmed successful call is visible as protocol success."""
    return (
        ToolResultStatus.SUCCESS
        if outcome.status is ToolOutcomeStatus.OK
        else ToolResultStatus.ERROR
    )


def bounded_outcome(
    outcome: ToolOutcome,
    *,
    maximum_bytes: int = MAX_TOOL_RESULT_BYTES,
) -> ToolOutcome:
    """Return a valid JSON envelope no larger than the configured byte limit."""
    if maximum_bytes < 512:
        raise ValueError("tool_result_limit_invalid")
    encoded = outcome.canonical_output().encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return outcome

    from common.json import truncate_utf8, utf8_size

    excerpt_limit = maximum_bytes - 512
    excerpt = truncate_utf8(dumps(outcome.data), max(0, excerpt_limit))
    candidate = outcome.model_copy(
        update={
            "message": outcome.message[:512],
            "data": {"truncated_json": excerpt},
            "truncated": True,
        }
    )
    while utf8_size(candidate.model_dump(mode="json")) > maximum_bytes:
        excerpt = excerpt[: max(0, len(excerpt) // 2)]
        candidate = candidate.model_copy(
            update={"data": {"truncated_json": excerpt} if excerpt else {}}
        )
    if utf8_size(candidate.model_dump(mode="json")) > maximum_bytes:
        candidate = candidate.model_copy(update={"message": "", "data": {}})
    return candidate


__all__ = [
    "AuthorizedRunContext",
    "AdmitToolCallCommand",
    "BeginToolCallBatchCommand",
    "BeginToolCallBatchItem",
    "BeginToolCallBatchResult",
    "ConvergeToolBatchCommand",
    "DecideToolApprovalCommand",
    "FinishToolCallCommand",
    "InitializeRunToolsCommand",
    "MAX_LLM_REQUESTS_PER_RUN",
    "MAX_TOOL_ARGUMENT_BYTES",
    "MAX_TOOL_CALL_ID_LENGTH",
    "MAX_TOOL_RESULT_BYTES",
    "ReserveLlmRequestCommand",
    "PendingToolApproval",
    "RunToolCallDetails",
    "RunToolCallPage",
    "RunToolCallRecord",
    "RunToolBatch",
    "RunToolBatchCall",
    "ToolApprovalDecision",
    "ToolCallPreparation",
    "ToolCallStatus",
    "ToolExecutionResult",
    "ToolExecutionContext",
    "ToolKind",
    "ToolOutcome",
    "ToolOutcomeStatus",
    "ToolRetryContext",
    "bounded_outcome",
    "persisted_status_for_outcome",
    "protocol_status_for_outcome",
]
