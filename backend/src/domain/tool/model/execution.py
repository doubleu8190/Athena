"""Trusted execution context, atomic commands, and protocol-safe outcomes."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
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
MAX_FORMATTED_ARGUMENT_VALUE_CHARS = 500
MAX_APPROVAL_SUMMARY_RENDERED_CHARS = 1000
_SUMMARY_PLACEHOLDER_PATTERN = re.compile(r"\{([^{}]+)\}")


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
    knowledge_base_ids: tuple[str, ...] | None = None


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
class FinishToolCallCommand:
    """Persist one tool call result, artifacts, delivery message, and skill audit atomically."""

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
    llm_request_count: int | None = None


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


class FormattedToolArgument(BaseModel):
    """面向业务审批人的工具参数可读键值项。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    label: str
    value: str


def _sanitize_display_text(value: str) -> str:
    """剔除除换行和制表符外的 Cc 控制字符，防止前端渲染或事件流被注入控制符。"""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        ch for ch in normalized if ch in {"\n", "\t"} or unicodedata.category(ch) != "Cc"
    )


def _truncate_text(value: str, maximum: int) -> str:
    """按字符上限安全截断超长展示文本。"""
    if len(value) <= maximum:
        return value
    if maximum <= 1:
        return "…"[:maximum]
    return value[: maximum - 1] + "…"


def format_argument_value(
    value: Any,
    *,
    max_length: int = MAX_FORMATTED_ARGUMENT_VALUE_CHARS,
) -> str:
    """将工具原始 JSON 参数值格式化为面向业务用户的安全可读文本。"""
    if value is None:
        return "无"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _truncate_text(_sanitize_display_text(value), max_length)
    if isinstance(value, list):
        if not value:
            return "无"
        if all(item is None or isinstance(item, (bool, int, float, str)) for item in value):
            rendered = "、".join(
                format_argument_value(item, max_length=max_length) for item in value
            )
            return _truncate_text(rendered, max_length)
        try:
            compact = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
        except (TypeError, ValueError):
            compact = str(value)
        return _truncate_text(_sanitize_display_text(compact), max_length)
    if isinstance(value, Mapping):
        if not value:
            return "无"
        try:
            compact = json.dumps(dict(value), ensure_ascii=False, separators=(", ", ": "))
        except (TypeError, ValueError):
            compact = str(value)
        return _truncate_text(_sanitize_display_text(compact), max_length)
    return _truncate_text(_sanitize_display_text(str(value)), max_length)


def format_tool_arguments(
    arguments: Mapping[str, Any],
    parameter_labels: Mapping[str, str],
) -> tuple[FormattedToolArgument, ...]:
    """生成业务审批参数键值表：已配置中文标签的参数按配置顺序优先展示，未映射参数回退原字段名。"""
    items: list[FormattedToolArgument] = []
    emitted: set[str] = set()
    # 1. 优先按管理员配置的 parameter_labels 顺序输出带中文标签的核心参数。
    for key, label in parameter_labels.items():
        if key in arguments:
            items.append(
                FormattedToolArgument(
                    key=key,
                    label=label,
                    value=format_argument_value(arguments[key]),
                )
            )
            emitted.add(key)
    # 2. 未配置映射的其余参数优雅回退为原字段名并追加在后，确保审批信息完整。
    for key, raw_value in arguments.items():
        if key in emitted:
            continue
        items.append(
            FormattedToolArgument(
                key=key,
                label=key,
                value=format_argument_value(raw_value),
            )
        )
    return tuple(items)


def render_approval_summary(
    template: str | None,
    arguments: Mapping[str, Any],
    *,
    max_length: int = MAX_APPROVAL_SUMMARY_RENDERED_CHARS,
) -> str | None:
    """使用纯正则 {key} 字典查表安全渲染审批摘要，严禁使用 str.format 防止属性或索引注入。"""
    if not template:
        return None
    cleaned = _sanitize_display_text(template).strip()
    if not cleaned:
        return None

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in arguments:
            return format_argument_value(arguments[key])
        return match.group(0)

    rendered = _SUMMARY_PLACEHOLDER_PATTERN.sub(_replace, cleaned)
    return _truncate_text(rendered, max_length)


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
    display_name: str | None = None
    approval_summary: str | None = None
    formatted_arguments: tuple[FormattedToolArgument, ...] = ()


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
            "message": truncate_utf8(outcome.message, 512),
            "data": {"truncated_json": excerpt} if excerpt else {},
            "truncated": True,
        }
    )
    # 仅在 excerpt 非空时折半缩短，避免 excerpt 耗尽后因长中文 message 陷入死循环。
    while excerpt and utf8_size(candidate.model_dump(mode="json")) > maximum_bytes:
        excerpt = excerpt[: max(0, len(excerpt) // 2)]
        candidate = candidate.model_copy(
            update={"data": {"truncated_json": excerpt} if excerpt else {}}
        )
    # 若清空 data 后仍超过字节上限，按 UTF-8 字节边界安全截断 message 再收敛。
    if utf8_size(candidate.model_dump(mode="json")) > maximum_bytes:
        candidate = candidate.model_copy(update={"data": {}})
        base_size = utf8_size(candidate.model_copy(update={"message": ""}).model_dump(mode="json"))
        message_budget = max(0, maximum_bytes - base_size)
        trimmed_message = truncate_utf8(candidate.message, message_budget)
        candidate = candidate.model_copy(update={"message": trimmed_message})
        while trimmed_message and utf8_size(candidate.model_dump(mode="json")) > maximum_bytes:
            message_budget //= 2
            trimmed_message = truncate_utf8(trimmed_message, message_budget)
            candidate = candidate.model_copy(update={"message": trimmed_message})
    if utf8_size(candidate.model_dump(mode="json")) > maximum_bytes:
        candidate = candidate.model_copy(update={"message": "", "data": {}})
    return candidate


__all__ = [
    "AuthorizedRunContext",
    "AdmitToolCallCommand",
    "BeginToolCallBatchCommand",
    "BeginToolCallBatchItem",
    "BeginToolCallBatchResult",
    "DecideToolApprovalCommand",
    "FinishToolCallCommand",
    "FormattedToolArgument",
    "InitializeRunToolsCommand",
    "MAX_APPROVAL_SUMMARY_RENDERED_CHARS",
    "MAX_FORMATTED_ARGUMENT_VALUE_CHARS",
    "MAX_LLM_REQUESTS_PER_RUN",
    "MAX_TOOL_ARGUMENT_BYTES",
    "MAX_TOOL_CALL_ID_LENGTH",
    "MAX_TOOL_RESULT_BYTES",
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
    "bounded_outcome",
    "format_argument_value",
    "format_tool_arguments",
    "persisted_status_for_outcome",
    "protocol_status_for_outcome",
    "render_approval_summary",
]
