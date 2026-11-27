"""Run HTTP 请求与响应 DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from api.dto.message import ArtifactFileRefResponse
from domain.run import InputRequestStatus, RunStatus
from domain.tool import ToolCallStatus, ToolKind, ToolOutcomeStatus


class RunSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=32_000)
    skill_ids: list[str] = Field(default_factory=list, max_length=100)
    input_file_ids: list[str] = Field(default_factory=list, max_length=100)
    knowledge_base_ids: list[str] | None = Field(default=None, max_length=20)
    model_profile_id: str | None = Field(default=None, min_length=1, max_length=64)


class RunAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=32_000)
    input_file_ids: list[str] = Field(default_factory=list, max_length=100)


class RunRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    confirm_unknown: bool = False


class RunToolApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    decision: Literal["approve", "reject"]


class SafeSkillInputFileResponse(BaseModel):
    file_id: str
    filename: str
    mime_type: str | None
    size_bytes: int | None
    available: bool


class SkillParameterSummaryItemResponse(BaseModel):
    name: str
    label: str
    value: str


class SafeSkillApprovalProjectionResponse(BaseModel):
    call_id: str
    skill_id: str
    skill_name: str
    skill_version_id: str
    skill_version_label: str
    script_label: str
    purpose: str
    input_files: list[SafeSkillInputFileResponse]
    parameter_summary: list[SkillParameterSummaryItemResponse]
    hidden_parameter_count: int
    can_approve: bool
    unavailable_reason: str | None


class FormattedToolArgumentResponse(BaseModel):
    """面向业务用户的待审批工具参数可读键值项。"""

    key: str
    label: str
    value: str


class RunModelSnapshotResponse(BaseModel):
    """Run 创建时固化的安全模型元数据只读投影，严禁暴露供应商地址与凭据信息。"""

    id: str
    name: str
    model: str
    scenario_label: str | None
    supports_vision: bool
    version: int


class RunPendingToolApprovalResponse(BaseModel):
    call_id: str
    batch_message_id: str
    ordinal: int
    model_name: str
    # 复用领域 ToolKind 枚举，收紧工具来源分类。
    tool_kind: ToolKind | None
    requested_at: datetime
    safe_skill_projection: SafeSkillApprovalProjectionResponse | None
    display_name: str | None
    approval_summary: str | None
    formatted_arguments: list[FormattedToolArgumentResponse]


class RunAttemptUsageResponse(BaseModel):
    execution_epoch: int
    attempt: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    complete: bool
    # 与领域 RunAttemptUsage.purpose 保持一致，收紧为回答或压缩两类用途。
    purpose: Literal["answer", "compression"]
    operation_id: str | None
    batch_index: int
    model_profile_id: str | None
    duration_ms: int | None


class RunUsageResponse(BaseModel):
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    complete: bool
    attempts: list[RunAttemptUsageResponse]
    answer_tokens: int | None
    compression_tokens: int | None


class RunInputRequestResponse(BaseModel):
    id: str
    question_message_id: str
    # 复用领域 InputRequestStatus 枚举，收紧输入请求状态。
    status: InputRequestStatus
    created_at: datetime


class RunInputInteractionResponse(BaseModel):
    input_request_id: str
    status: InputRequestStatus
    question_text: str
    answer_text: str | None
    created_at: datetime
    answered_at: datetime | None


class RunSkillEntryResponse(BaseModel):
    skill_id: str
    slug: str
    # 与 SkillSummaryResponse.scope 及领域 SkillScope 保持一致。
    scope: Literal["company", "department", "personal"]
    name: str
    description: str
    version_id: str
    version_label: str
    requires_approval: bool


class RunSkillSnapshotResponse(BaseModel):
    version: int
    skills: list[RunSkillEntryResponse]
    selected_skill_ids: list[str]


class SubagentTaskResponse(BaseModel):
    sub_run_id: str
    parent_tool_call_id: str
    subagent_profile_id: str | None
    subagent_slug: str
    subagent_name: str
    task_index: int
    task_summary: str
    # 复用领域 RunStatus 枚举，与主 Run 状态机保持一致。
    status: RunStatus
    safe_error_code: str | None
    created_at: datetime
    first_started_at: datetime | None
    finished_at: datetime | None


class PendingSubagentInputResponse(BaseModel):
    sub_run_id: str
    subagent_slug: str
    subagent_name: str
    task_index: int
    task_summary: str
    input_request_id: str
    question_message_id: str
    question_text: str
    created_at: datetime


class PendingSubagentApprovalResponse(BaseModel):
    sub_run_id: str
    subagent_slug: str
    subagent_name: str
    task_index: int
    task_summary: str
    call_id: str
    batch_message_id: str
    ordinal: int
    model_name: str
    tool_kind: ToolKind | None
    requested_at: datetime
    safe_skill_projection: SafeSkillApprovalProjectionResponse | None
    display_name: str | None
    approval_summary: str | None
    formatted_arguments: list[FormattedToolArgumentResponse]


class RunResponse(BaseModel):
    id: str
    session_id: str
    status: RunStatus
    trigger_message_id: str
    context_cutoff_sequence: int
    retry_of_run_id: str | None
    model_config_version_id: str
    model_snapshot: RunModelSnapshotResponse
    prompt_template_version: str
    event_sequence: int
    execution_epoch: int
    queued_at: datetime
    first_started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
    cancel_requested_at: datetime | None
    safe_error_code: str | None
    report_message_id: str | None
    assistant_message_id: str | None
    usage: RunUsageResponse
    skill_snapshot: RunSkillSnapshotResponse | None
    input_file_ids: list[str]
    knowledge_base_ids: list[str] | None
    pending_input: RunInputRequestResponse | None
    pending_approvals: list[RunPendingToolApprovalResponse]
    parent_run_id: str | None
    root_run_id: str | None
    parent_tool_call_id: str | None
    depth: int
    subagent_profile_id: str | None
    subagent_slug: str | None
    subagent_name: str | None
    subagent_tasks: list[SubagentTaskResponse]
    pending_subagent_inputs: list[PendingSubagentInputResponse]
    pending_subagent_approvals: list[PendingSubagentApprovalResponse]
    input_interactions: list[RunInputInteractionResponse]
    replayed: bool
    retry_warning: str | None


class RunListResponse(BaseModel):
    items: list[RunResponse]
    next_cursor: str | None


class RunEventResponse(BaseModel):
    id: str
    run_id: str
    sequence: int
    type: str
    created_at: datetime
    payload: dict[str, object]


class RunEventListResponse(BaseModel):
    items: list[RunEventResponse]
    next_after_sequence: int | None


class RunToolOutcomeResponse(BaseModel):
    # 复用领域 ToolOutcomeStatus / ToolKind / ToolCallStatus 枚举，收紧工具执行结果与调用状态。
    status: ToolOutcomeStatus
    code: str
    message: str
    data: dict[str, object]
    truncated: bool
    cleanup_error_code: str | None


class RunToolCallSummaryResponse(BaseModel):
    id: str
    tool_call_id: str
    ordinal: int
    tool_id: str | None
    tool_kind: ToolKind | None
    model_name: str
    status: ToolCallStatus
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    safe_error_code: str | None
    truncated: bool


class RunToolCallDetailResponse(RunToolCallSummaryResponse):
    arguments: dict[str, object] | None
    outcome: RunToolOutcomeResponse | None
    artifacts: list[ArtifactFileRefResponse]
    safe_skill_projection: SafeSkillApprovalProjectionResponse | None


class RunToolCallListResponse(BaseModel):
    items: list[RunToolCallSummaryResponse]
    next_after_ordinal: int | None


__all__ = [
    "FormattedToolArgumentResponse",
    "PendingSubagentApprovalResponse",
    "PendingSubagentInputResponse",
    "RunAnswerRequest",
    "RunModelSnapshotResponse",
    "SubagentTaskResponse",
    "RunAttemptUsageResponse",
    "RunEventListResponse",
    "RunEventResponse",
    "RunInputInteractionResponse",
    "RunInputRequestResponse",
    "RunListResponse",
    "RunPendingToolApprovalResponse",
    "RunResponse",
    "RunSkillEntryResponse",
    "RunSkillSnapshotResponse",
    "RunRetryRequest",
    "RunSubmitRequest",
    "RunToolCallDetailResponse",
    "RunToolCallListResponse",
    "RunToolCallSummaryResponse",
    "RunToolApprovalRequest",
    "RunToolOutcomeResponse",
    "RunUsageResponse",
    "SafeSkillApprovalProjectionResponse",
    "SafeSkillInputFileResponse",
    "SkillParameterSummaryItemResponse",
]
