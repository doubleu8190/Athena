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
from domain.preference import SAVE_USER_MEMORY_TOOL_ID
from domain.skill import (
    SESSION_FILE_LIST_TOOL_ID,
    SESSION_FILE_READ_TOOL_ID,
    SKILL_EXECUTE_TOOL_ID,
    SKILL_LOAD_TOOL_ID,
    SKILL_READ_RESOURCE_TOOL_ID,
    RunSkillSnapshot,
    SafeSkillApprovalProjection,
    SkillExecutionCompletion,
)
from domain.subagent import DISPATCH_SUBAGENTS_TOOL_ID

if TYPE_CHECKING:
    from domain.tool.model.runtime import RuntimeToolSnapshot

MAX_TOOL_ARGUMENT_BYTES = 64 * 1024
MAX_TOOL_RESULT_BYTES = 64 * 1024
MAX_TOOL_CALL_ID_LENGTH = 256
MAX_LLM_REQUESTS_PER_RUN = 40
MAX_FORMATTED_ARGUMENT_VALUE_CHARS = 500
MAX_APPROVAL_SUMMARY_RENDERED_CHARS = 1000
_SUMMARY_PLACEHOLDER_PATTERN = re.compile(r"\{([^{}]+)\}")
_KNOWLEDGE_SEARCH_TOOL_ID = "builtin_knowledge_search"
_REQUEST_USER_INPUT_TOOL_ID = "builtin_request_user_input"
_SKILL_TOOL_IDS = frozenset(
    {
        SKILL_EXECUTE_TOOL_ID,
        SKILL_LOAD_TOOL_ID,
        SKILL_READ_RESOURCE_TOOL_ID,
    }
)
_SKILL_MODEL_NAMES = frozenset(
    {
        "skill_execute",
        "skill_load",
        "skill_read_resource",
    }
)
_BUILTIN_DISPLAY_NAMES: dict[str, str] = {
    _KNOWLEDGE_SEARCH_TOOL_ID: "查阅企业知识库",
    "knowledge_search": "查阅企业知识库",
    SESSION_FILE_LIST_TOOL_ID: "查看会话文件",
    "session_file_list": "查看会话文件",
    SESSION_FILE_READ_TOOL_ID: "读取会话文件",
    "session_file_read": "读取会话文件",
    SAVE_USER_MEMORY_TOOL_ID: "记录个人工作偏好",
    "save_user_memory": "记录个人工作偏好",
    DISPATCH_SUBAGENTS_TOOL_ID: "协调协作助手",
    "dispatch_subagents": "协调协作助手",
    _REQUEST_USER_INPUT_TOOL_ID: "请求补充信息",
    "request_user_input": "请求补充信息",
    SKILL_LOAD_TOOL_ID: "加载技能指南",
    "skill_load": "加载技能指南",
    SKILL_READ_RESOURCE_TOOL_ID: "读取技能资源",
    "skill_read_resource": "读取技能资源",
    SKILL_EXECUTE_TOOL_ID: "执行技能脚本",
    "skill_execute": "执行技能脚本",
}
_TIMEOUT_ERROR_CODES = frozenset(
    {
        "mcp_deadline_exhausted",
        "mcp_timeout",
        "mcp_tool_timeout",
        "tool_deadline_exhausted",
        "tool_timeout",
    }
)


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
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class RunToolCallDetails:
    """Authorized detail projection with bounded protocol payloads."""

    record: RunToolCallRecord
    arguments: dict[str, Any]
    output: str | None
    artifacts: tuple[ArtifactFileRef, ...] = ()
    safe_skill_projection: SafeSkillApprovalProjection | None = None
    display_name: str | None = None
    business_summary: str | None = None
    formatted_arguments: tuple[FormattedToolArgument, ...] = ()
    hidden_argument_count: int = 0
    safe_outcome_summary: str | None = None
    formatted_results: tuple[FormattedToolArgument, ...] = ()


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


@dataclass(frozen=True, slots=True)
class SafeToolCallProjection:
    """面向普通用户的工具调用安全业务投影，不含原始入参/出参与内部标识。"""

    display_name: str | None = None
    business_summary: str | None = None
    formatted_arguments: tuple[FormattedToolArgument, ...] = ()
    hidden_argument_count: int = 0
    safe_outcome_summary: str | None = None
    formatted_results: tuple[FormattedToolArgument, ...] = ()


def is_skill_tool_call(
    tool_id: str | None,
    model_name: str,
    tool_kind: ToolKind | str | None = None,
) -> bool:
    """按工具 ID、工具类别与归一化 model_name 识别 Skill 调用，确保绝不读取原始脚本参数。"""
    if tool_id in _SKILL_TOOL_IDS:
        return True
    kind_value = tool_kind.value if isinstance(tool_kind, ToolKind) else tool_kind
    # 显式标记为 MCP 工具时跳过名称后缀推断，避免与远端 MCP 工具命名混淆。
    if kind_value == ToolKind.MCP.value:
        return False
    normalized = model_name.strip().lower()
    return normalized in _SKILL_MODEL_NAMES or any(
        normalized.endswith(f"_{name}") for name in _SKILL_MODEL_NAMES
    )


def resolve_tool_display_name(
    *,
    tool_id: str | None,
    tool_kind: ToolKind | str | None,
    model_name: str,
    safe_skill_projection: SafeSkillApprovalProjection | None = None,
    meta_display_name: str | None = None,
) -> str | None:
    """统一解析面向普通用户展示的工具业务名称。"""
    if is_skill_tool_call(tool_id, model_name, tool_kind):
        normalized = model_name.strip().lower()
        if (
            tool_id == SKILL_LOAD_TOOL_ID
            or normalized == "skill_load"
            or normalized.endswith("_skill_load")
        ):
            return _BUILTIN_DISPLAY_NAMES[SKILL_LOAD_TOOL_ID]
        if (
            tool_id == SKILL_READ_RESOURCE_TOOL_ID
            or normalized == "skill_read_resource"
            or normalized.endswith("_skill_read_resource")
        ):
            return _BUILTIN_DISPLAY_NAMES[SKILL_READ_RESOURCE_TOOL_ID]
        if safe_skill_projection is not None and safe_skill_projection.script_label:
            return safe_skill_projection.script_label
        return _BUILTIN_DISPLAY_NAMES[SKILL_EXECUTE_TOOL_ID]
    kind_value = tool_kind.value if isinstance(tool_kind, ToolKind) else tool_kind
    if kind_value != ToolKind.MCP.value:
        if tool_id and tool_id in _BUILTIN_DISPLAY_NAMES:
            return _BUILTIN_DISPLAY_NAMES[tool_id]
        if model_name in _BUILTIN_DISPLAY_NAMES:
            return _BUILTIN_DISPLAY_NAMES[model_name]
    return meta_display_name


def project_whitelisted_fields(
    payload: Mapping[str, Any],
    labels: Mapping[str, str],
) -> tuple[tuple[FormattedToolArgument, ...], int]:
    """按白名单标签映射严格投影可读字段，未映射字段仅计入隐藏计数绝不透出原键名。"""
    items = tuple(
        FormattedToolArgument(
            key=key,
            label=label,
            value=format_argument_value(payload[key]),
        )
        for key, label in labels.items()
        if key in payload
    )
    hidden_count = sum(1 for key in payload if key not in labels)
    return items, hidden_count


def format_tool_arguments(
    arguments: Mapping[str, Any],
    parameter_labels: Mapping[str, str],
) -> tuple[FormattedToolArgument, ...]:
    """按参数白名单标签生成可读键值表，未配置标签的字段不再回退透出原英文键名。"""
    items, _ = project_whitelisted_fields(arguments, parameter_labels)
    return items


def project_mcp_whitelisted_results(
    outcome_data: Mapping[str, Any],
    result_labels: Mapping[str, str],
) -> tuple[FormattedToolArgument, ...]:
    """按管理员配置的 result_labels 白名单精确抽取 MCP 结果字段，绝不透出未命中字段或原始文本。"""
    if not result_labels or not outcome_data:
        return ()
    target_mapping: Mapping[str, Any] | None = None
    structured = outcome_data.get("structured")
    if isinstance(structured, Mapping):
        target_mapping = structured
    else:
        raw_text = outcome_data.get("text")
        if isinstance(raw_text, str) and raw_text.strip():
            try:
                parsed = json.loads(raw_text)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                target_mapping = parsed
    if not target_mapping:
        return ()
    items, _ = project_whitelisted_fields(target_mapping, result_labels)
    return items


def _safe_skill_outcome_summary(
    outcome: ToolOutcome | None,
    safe_error_code: str | None,
) -> str | None:
    """将 Skill 工具结果收口为确定性中文状态文案，严禁透出原始日志、路径或异常文本。"""
    if outcome is None:
        return None
    if outcome.status is ToolOutcomeStatus.OK:
        if outcome.code == "skill_loaded":
            return "已加载技能指南。"
        if outcome.code == "skill_resource_read":
            return "已读取技能资源。"
        return "已完成技能脚本执行（部分内容已省略）。" if outcome.truncated else "已完成技能执行。"
    if outcome.status is ToolOutcomeStatus.CANCELLED:
        return "技能执行已取消。"
    if outcome.status is ToolOutcomeStatus.UNKNOWN:
        return "技能执行状态未知，请核实后重试。"
    code = safe_error_code or outcome.code
    error_messages = {
        "tool_approval_denied": "已拒绝执行该技能操作。",
        "tool_permission_denied": "当前账号无权使用该技能。",
        "skill_not_in_run_snapshot": "该技能不在当前任务可用范围中。",
        "skill_version_unavailable": "该技能版本当前不可用。",
        "skill_script_not_registered": "请求的技能脚本未登记。",
        "skill_parameters_invalid": "技能调用参数校验未通过。",
        "tool_arguments_invalid": "技能调用参数校验未通过。",
        "skill_dependency_unsatisfied": "当前运行环境不满足技能依赖。",
        "session_file_not_found": "输入文件不存在或无权访问。",
        "session_file_quota_exceeded": "当前会话文件数量已达上限，无法保存产物。",
        "skill_resource_not_found": "请求的技能资源不存在。",
        "skill_resource_not_text": "请求的技能资源不支持文本读取。",
        "skill_resource_offset_invalid": "技能资源读取范围无效或超过上限。",
        "skill_resource_range_invalid": "技能资源读取范围无效或超过上限。",
        "skill_resource_too_large": "技能资源读取范围无效或超过上限。",
        "tool_unavailable": "该技能能力当前不可用。",
    }
    if code in _TIMEOUT_ERROR_CODES:
        return "技能执行超时。"
    return error_messages.get(code, "技能执行失败。")


def _safe_mcp_outcome_summary(
    outcome: ToolOutcome | None,
    safe_error_code: str | None,
) -> str | None:
    """将 MCP 工具结果映射为确定性中文业务状态文案，严禁透传原始文本或远端返回数据。"""
    if outcome is None:
        return None
    if outcome.status is ToolOutcomeStatus.OK:
        return (
            "已完成业务工具调用（部分内容已省略）。"
            if outcome.truncated
            else "已完成业务工具调用。"
        )
    if outcome.status is ToolOutcomeStatus.CANCELLED:
        return "业务工具调用已取消。"
    if outcome.status is ToolOutcomeStatus.UNKNOWN:
        return "业务工具执行结果未知，请先核实外部系统状态。"
    code = safe_error_code or outcome.code
    if code == "tool_approval_denied":
        return "已拒绝执行该业务操作。"
    if code == "tool_permission_denied":
        return "当前账号无权执行该业务操作。"
    if code in {"tool_unavailable", "tool_unknown", "tool_version_unavailable"}:
        return "该业务工具当前不可用或配置已变更。"
    if code in {"tool_arguments_invalid", "tool_arguments_too_large"}:
        return "业务调用参数校验未通过。"
    if code in _TIMEOUT_ERROR_CODES:
        return "业务工具调用超时。"
    return "业务工具返回执行失败。"


def _project_knowledge_search(
    display_name: str | None,
    arguments: Mapping[str, Any],
    outcome: ToolOutcome | None,
    safe_error_code: str | None,
) -> SafeToolCallProjection:
    """为 knowledge_search 生成白名单入参、来源标题与统计摘要，严禁透出内部 ID 与原文片段。"""
    formatted_args, hidden_count = project_whitelisted_fields(arguments, {"query": "检索内容"})
    raw_query = arguments.get("query")
    business_summary = (
        f"检索知识库：{format_argument_value(raw_query.strip(), max_length=100)}"
        if isinstance(raw_query, str) and raw_query.strip()
        else None
    )
    if outcome is None:
        return SafeToolCallProjection(
            display_name=display_name,
            business_summary=business_summary,
            formatted_arguments=formatted_args,
            hidden_argument_count=hidden_count,
        )
    formatted_results: tuple[FormattedToolArgument, ...] = ()
    if outcome.status is ToolOutcomeStatus.OK:
        raw_items = outcome.data.get("items")
        items = raw_items if isinstance(raw_items, list) else []
        hit_count = len(items)
        degraded = outcome.code == "knowledge_degraded" or outcome.data.get("status") == "degraded"
        if degraded:
            summary = f"检索结果可能不完整（已找到 {hit_count} 条相关片段）。"
        elif hit_count == 0:
            summary = "未找到相关知识片段。"
        else:
            summary = f"已找到 {hit_count} 条相关知识片段。"
        titles = list(
            dict.fromkeys(
                _truncate_text(_sanitize_display_text(item["title"].strip()), 120)
                for item in items
                if isinstance(item, Mapping)
                and isinstance(item.get("title"), str)
                and item["title"].strip()
            )
        )
        if titles:
            formatted_results = (
                FormattedToolArgument(
                    key="sources",
                    label="来源文档",
                    value=_truncate_text(
                        "、".join(titles[:10]), MAX_FORMATTED_ARGUMENT_VALUE_CHARS
                    ),
                ),
            )
    elif outcome.status is ToolOutcomeStatus.CANCELLED:
        summary = "知识库检索已取消。"
    elif outcome.status is ToolOutcomeStatus.UNKNOWN:
        summary = "知识库检索状态未知。"
    else:
        code = safe_error_code or outcome.code
        error_messages = {
            "tool_arguments_invalid": "检索参数校验未通过。",
            "tool_permission_denied": "当前账号无权检索知识库。",
            "knowledge_scope_unavailable": "当前对话知识库范围不可用。",
            "knowledge_unavailable": "知识库检索服务当前不可用。",
            "tool_unavailable": "知识库检索服务当前不可用。",
        }
        summary = (
            "知识库检索超时。"
            if code in _TIMEOUT_ERROR_CODES
            else error_messages.get(code, "知识库检索失败。")
        )
    return SafeToolCallProjection(
        display_name=display_name,
        business_summary=business_summary,
        formatted_arguments=formatted_args,
        hidden_argument_count=hidden_count,
        safe_outcome_summary=summary,
        formatted_results=formatted_results,
    )


def _project_session_file_list(
    display_name: str | None,
    arguments: Mapping[str, Any],
    outcome: ToolOutcome | None,
    safe_error_code: str | None,
) -> SafeToolCallProjection:
    """为 session_file_list 生成文件计数与文件名白名单投影，严禁暴露 file_id 等元数据。"""
    formatted_args, hidden_count = project_whitelisted_fields(arguments, {})
    if outcome is None:
        return SafeToolCallProjection(
            display_name=display_name,
            formatted_arguments=formatted_args,
            hidden_argument_count=hidden_count,
        )
    formatted_results: tuple[FormattedToolArgument, ...] = ()
    if outcome.status is ToolOutcomeStatus.OK:
        raw_items = outcome.data.get("items")
        items = raw_items if isinstance(raw_items, list) else []
        summary = f"已列出当前会话 {len(items)} 个文件。"
        filenames = [
            _truncate_text(_sanitize_display_text(item["filename"].strip()), 120)
            for item in items
            if isinstance(item, Mapping)
            and isinstance(item.get("filename"), str)
            and item["filename"].strip()
        ]
        if filenames:
            formatted_results = (
                FormattedToolArgument(
                    key="filenames",
                    label="文件列表",
                    value=_truncate_text(
                        "、".join(filenames[:10]), MAX_FORMATTED_ARGUMENT_VALUE_CHARS
                    ),
                ),
            )
    elif outcome.status is ToolOutcomeStatus.CANCELLED:
        summary = "查看会话文件已取消。"
    elif outcome.status is ToolOutcomeStatus.UNKNOWN:
        summary = "查看会话文件状态未知。"
    else:
        code = safe_error_code or outcome.code
        error_messages = {
            "session_file_cursor_invalid": "会话文件列表参数无效。",
            "tool_arguments_invalid": "会话文件列表参数无效。",
            "file_tool_unavailable": "会话文件服务当前不可用。",
            "session_file_unavailable": "会话文件服务当前不可用。",
            "tool_unavailable": "会话文件服务当前不可用。",
            "tool_permission_denied": "当前账号无权查看会话文件。",
        }
        summary = error_messages.get(code, "查看会话文件失败。")
    return SafeToolCallProjection(
        display_name=display_name,
        formatted_arguments=formatted_args,
        hidden_argument_count=hidden_count,
        safe_outcome_summary=summary,
        formatted_results=formatted_results,
    )


def _project_session_file_read(
    display_name: str | None,
    arguments: Mapping[str, Any],
    outcome: ToolOutcome | None,
    safe_error_code: str | None,
) -> SafeToolCallProjection:
    """为 session_file_read 仅投影文件名与安全状态摘要，严禁暴露正文、偏移量与 file_id。"""
    formatted_args, hidden_count = project_whitelisted_fields(arguments, {})
    if outcome is None:
        return SafeToolCallProjection(
            display_name=display_name,
            formatted_arguments=formatted_args,
            hidden_argument_count=hidden_count,
        )
    file_obj = outcome.data.get("file")
    filename = (
        _truncate_text(_sanitize_display_text(file_obj["filename"].strip()), 120)
        if isinstance(file_obj, Mapping)
        and isinstance(file_obj.get("filename"), str)
        and file_obj["filename"].strip()
        else None
    )
    formatted_results = (
        (FormattedToolArgument(key="filename", label="文件名", value=filename),)
        if filename is not None
        else ()
    )
    if outcome.status is ToolOutcomeStatus.OK:
        summary = f"已读取会话文件《{filename}》。" if filename else "已读取会话文件。"
    elif outcome.status is ToolOutcomeStatus.CANCELLED:
        summary = "读取会话文件已取消。"
    elif outcome.status is ToolOutcomeStatus.UNKNOWN:
        summary = "读取会话文件状态未知。"
    else:
        code = safe_error_code or outcome.code
        error_messages = {
            "file_not_text": "该文件不是可直接读取的文本或办公文档。",
            "file_offset_invalid": "文件读取范围参数无效。",
            "file_range_too_small": "文件读取范围参数无效。",
            "tool_arguments_invalid": "文件读取范围参数无效。",
            "file_content_not_text": "文件内容不是合法文本，无法读取。",
            "document_no_extractable_text": "该文档未检测到可提取的文本内容。",
            "document_encrypted": "该文档已加密，无法自动提取文本。",
            "document_macro_unsupported": "包含宏的文档不支持自动提取文本。",
            "document_page_limit_exceeded": "该文档超过自动文本提取限制。",
            "document_extract_too_large": "该文档超过自动文本提取限制。",
            "document_extracted_text_too_large": "该文档超过自动文本提取限制。",
            "document_archive_limit_exceeded": "该文档超过自动文本提取限制。",
            "document_extract_timeout": "文档文本读取超时。",
            "document_parse_failed": "文档解析失败，无法读取文本。",
            "file_tool_unavailable": "会话文件服务当前不可用。",
            "session_file_unavailable": "会话文件服务当前不可用。",
            "tool_unavailable": "会话文件服务当前不可用。",
            "session_file_read_failed": "会话文件读取失败。",
            "tool_permission_denied": "当前账号无权读取该会话文件。",
        }
        summary = (
            "文档文本读取超时。"
            if code in _TIMEOUT_ERROR_CODES
            else error_messages.get(code, "读取会话文件失败。")
        )
    return SafeToolCallProjection(
        display_name=display_name,
        formatted_arguments=formatted_args,
        hidden_argument_count=hidden_count,
        safe_outcome_summary=summary,
        formatted_results=formatted_results,
    )


def _project_save_user_memory(
    display_name: str | None,
    arguments: Mapping[str, Any],
    outcome: ToolOutcome | None,
    safe_error_code: str | None,
) -> SafeToolCallProjection:
    """为 save_user_memory 仅投影记忆内容与安全状态，隐藏内部 ID 与时间戳。"""
    formatted_args, hidden_count = project_whitelisted_fields(arguments, {"content": "记忆内容"})
    if outcome is None:
        summary = None
    elif outcome.status is ToolOutcomeStatus.OK:
        summary = "已保存个人工作偏好。"
    elif outcome.status is ToolOutcomeStatus.CANCELLED:
        summary = "保存个人工作偏好已取消。"
    elif outcome.status is ToolOutcomeStatus.UNKNOWN:
        summary = "保存个人工作偏好状态未知。"
    else:
        code = safe_error_code or outcome.code
        error_messages = {
            "user_memory_disabled": "未开启个人工作偏好记忆功能。",
            "user_memory_limit_reached": "个人工作偏好记忆数量已达上限。",
            "user_memory_content_invalid": "记忆内容格式不合法。",
            "tool_arguments_invalid": "记忆内容格式不合法。",
            "user_memory_unavailable": "个人工作偏好记忆服务当前不可用。",
            "tool_unavailable": "个人工作偏好记忆服务当前不可用。",
        }
        summary = error_messages.get(code, "保存个人工作偏好失败。")
    return SafeToolCallProjection(
        display_name=display_name,
        formatted_arguments=formatted_args,
        hidden_argument_count=hidden_count,
        safe_outcome_summary=summary,
    )


def _project_dispatch_subagents(
    display_name: str | None,
    arguments: Mapping[str, Any],
    outcome: ToolOutcome | None,
    safe_error_code: str | None,
) -> SafeToolCallProjection:
    """为 dispatch_subagents 仅投影任务计数摘要，严禁暴露子任务内部 ID、slug 与结果正文。"""
    formatted_args, hidden_count = project_whitelisted_fields(arguments, {})
    tasks = arguments.get("tasks")
    business_summary = (
        f"已派发 {len(tasks)} 项协作任务" if isinstance(tasks, list) and len(tasks) > 0 else None
    )
    if outcome is None:
        summary = None
    elif outcome.status is ToolOutcomeStatus.OK:
        completed = outcome.data.get("completed_count")
        failed = outcome.data.get("failed_count")
        if (
            isinstance(completed, int)
            and not isinstance(completed, bool)
            and isinstance(failed, int)
            and not isinstance(failed, bool)
        ):
            summary = f"协作任务已完成（成功 {completed}，失败 {failed}）。"
        else:
            summary = "协作任务已完成。"
    elif outcome.status is ToolOutcomeStatus.CANCELLED:
        summary = "协作任务已取消。"
    elif outcome.status is ToolOutcomeStatus.UNKNOWN:
        summary = "协作任务执行状态未知。"
    else:
        code = safe_error_code or outcome.code
        error_messages = {
            "tool_arguments_invalid": "协作任务参数校验未通过。",
            "tool_permission_denied": "当前账号无权调用协作助手。",
            "tool_unavailable": "协作助手当前不可用。",
        }
        summary = error_messages.get(code, "协作任务执行失败。")
    return SafeToolCallProjection(
        display_name=display_name,
        business_summary=business_summary,
        formatted_arguments=formatted_args,
        hidden_argument_count=hidden_count,
        safe_outcome_summary=summary,
    )


def _project_request_user_input(
    display_name: str | None,
    arguments: Mapping[str, Any],
    outcome: ToolOutcome | None,
) -> SafeToolCallProjection:
    """为 request_user_input 仅投影澄清问题与安全状态。"""
    formatted_args, hidden_count = project_whitelisted_fields(arguments, {"question": "澄清问题"})
    if outcome is None:
        summary = None
    elif outcome.status is ToolOutcomeStatus.OK:
        summary = "已收集用户补充信息。"
    elif outcome.status is ToolOutcomeStatus.CANCELLED:
        summary = "请求补充信息已取消。"
    elif outcome.status is ToolOutcomeStatus.UNKNOWN:
        summary = "请求补充信息状态未知。"
    else:
        summary = "请求补充信息未完成。"
    return SafeToolCallProjection(
        display_name=display_name,
        formatted_arguments=formatted_args,
        hidden_argument_count=hidden_count,
        safe_outcome_summary=summary,
    )


def build_safe_tool_call_projection(
    *,
    tool_id: str | None,
    tool_kind: ToolKind | None,
    model_name: str,
    arguments: Mapping[str, Any],
    outcome: ToolOutcome | None,
    safe_error_code: str | None = None,
    safe_skill_projection: SafeSkillApprovalProjection | None = None,
    meta_display_name: str | None = None,
    meta_template: str | None = None,
    meta_parameter_labels: Mapping[str, str] | None = None,
    meta_result_labels: Mapping[str, str] | None = None,
) -> SafeToolCallProjection:
    """统一为 Skill、平台内置工具与 MCP 业务工具构建严格白名单安全投影。"""
    display_name = resolve_tool_display_name(
        tool_id=tool_id,
        tool_kind=tool_kind,
        model_name=model_name,
        safe_skill_projection=safe_skill_projection,
        meta_display_name=meta_display_name,
    )

    # 1. Skill 工具：严格隔离原始脚本参数、路径与日志，仅输出确定性中文状态摘要。
    if is_skill_tool_call(tool_id, model_name, tool_kind):
        return SafeToolCallProjection(
            display_name=display_name,
            business_summary=None,
            formatted_arguments=(),
            hidden_argument_count=(
                safe_skill_projection.hidden_parameter_count
                if safe_skill_projection is not None
                else 0
            ),
            safe_outcome_summary=_safe_skill_outcome_summary(outcome, safe_error_code),
            formatted_results=(),
        )

    # 2. 平台内置工具：使用服务端固定白名单，绝不透传内部 ID 或原始文档/文件正文。
    if tool_kind is not ToolKind.MCP:
        if tool_id == _KNOWLEDGE_SEARCH_TOOL_ID or model_name == "knowledge_search":
            return _project_knowledge_search(display_name, arguments, outcome, safe_error_code)
        if tool_id == SESSION_FILE_LIST_TOOL_ID or model_name == "session_file_list":
            return _project_session_file_list(display_name, arguments, outcome, safe_error_code)
        if tool_id == SESSION_FILE_READ_TOOL_ID or model_name == "session_file_read":
            return _project_session_file_read(display_name, arguments, outcome, safe_error_code)
        if tool_id == SAVE_USER_MEMORY_TOOL_ID or model_name == "save_user_memory":
            return _project_save_user_memory(display_name, arguments, outcome, safe_error_code)
        if tool_id == DISPATCH_SUBAGENTS_TOOL_ID or model_name == "dispatch_subagents":
            return _project_dispatch_subagents(display_name, arguments, outcome, safe_error_code)
        if tool_id == _REQUEST_USER_INPUT_TOOL_ID or model_name == "request_user_input":
            return _project_request_user_input(display_name, arguments, outcome)
        if tool_kind is ToolKind.BUILTIN:
            formatted_args, hidden_count = project_whitelisted_fields(arguments, {})
            if outcome is None:
                generic_summary = None
            elif outcome.status is ToolOutcomeStatus.OK:
                generic_summary = "已完成内置工具调用。"
            elif outcome.status is ToolOutcomeStatus.CANCELLED:
                generic_summary = "内置工具调用已取消。"
            elif outcome.status is ToolOutcomeStatus.UNKNOWN:
                generic_summary = "内置工具执行状态未知。"
            else:
                generic_summary = "内置工具调用失败。"
            return SafeToolCallProjection(
                display_name=display_name,
                formatted_arguments=formatted_args,
                hidden_argument_count=hidden_count,
                safe_outcome_summary=generic_summary,
            )

    # 3. MCP 业务工具（及未识别类别兜底）：严格按管理员白名单投影入参/出参并收口状态摘要。
    formatted_args, hidden_count = project_whitelisted_fields(
        arguments, meta_parameter_labels or {}
    )
    business_summary = render_approval_summary(meta_template, arguments)
    safe_outcome_summary = _safe_mcp_outcome_summary(outcome, safe_error_code)
    formatted_results = (
        project_mcp_whitelisted_results(outcome.data, meta_result_labels or {})
        if outcome is not None
        else ()
    )
    return SafeToolCallProjection(
        display_name=display_name,
        business_summary=business_summary,
        formatted_arguments=formatted_args,
        hidden_argument_count=hidden_count,
        safe_outcome_summary=safe_outcome_summary,
        formatted_results=formatted_results,
    )


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
    hidden_argument_count: int = 0


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
    "SafeToolCallProjection",
    "ToolApprovalDecision",
    "ToolCallPreparation",
    "ToolCallStatus",
    "ToolExecutionResult",
    "ToolExecutionContext",
    "ToolKind",
    "ToolOutcome",
    "ToolOutcomeStatus",
    "bounded_outcome",
    "build_safe_tool_call_projection",
    "format_argument_value",
    "format_tool_arguments",
    "is_skill_tool_call",
    "persisted_status_for_outcome",
    "project_mcp_whitelisted_results",
    "project_whitelisted_fields",
    "protocol_status_for_outcome",
    "render_approval_summary",
    "resolve_tool_display_name",
]
