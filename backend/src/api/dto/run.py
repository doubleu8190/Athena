"""Run HTTP 请求与响应 DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from api.dto.message import ArtifactFileRefResponse


class RunSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=32_000)
    skill_ids: list[str] = Field(default_factory=list, max_length=100)
    input_file_ids: list[str] = Field(default_factory=list, max_length=100)


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
    mime_type: str | None = None
    size_bytes: int | None = None
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
    input_files: list[SafeSkillInputFileResponse] = Field(default_factory=list)
    parameter_summary: list[SkillParameterSummaryItemResponse] = Field(default_factory=list)
    hidden_parameter_count: int = 0
    can_approve: bool = True
    unavailable_reason: str | None = None


class RunPendingToolApprovalResponse(BaseModel):
    call_id: str
    batch_message_id: str
    ordinal: int
    model_name: str
    tool_kind: str | None
    requested_at: datetime
    safe_skill_projection: SafeSkillApprovalProjectionResponse | None = None


class RunAttemptUsageResponse(BaseModel):
    execution_epoch: int
    attempt: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    complete: bool
    purpose: str = "answer"
    operation_id: str | None = None
    batch_index: int = 0
    model_profile_id: str | None = None
    duration_ms: int | None = None


class RunUsageResponse(BaseModel):
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    complete: bool
    attempts: list[RunAttemptUsageResponse]
    answer_tokens: int | None = None
    compression_tokens: int | None = None


class RunInputRequestResponse(BaseModel):
    id: str
    question_message_id: str
    status: str
    created_at: datetime


class RunSkillEntryResponse(BaseModel):
    skill_id: str
    slug: str
    name: str
    description: str
    version_id: str
    version_label: str
    requires_approval: bool


class RunSkillSnapshotResponse(BaseModel):
    version: int
    skills: list[RunSkillEntryResponse]
    selected_skill_ids: list[str]


class RunResponse(BaseModel):
    id: str
    session_id: str
    status: str
    trigger_message_id: str
    context_cutoff_sequence: int
    retry_of_run_id: str | None
    model_config_version_id: str
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
    usage: RunUsageResponse
    skill_snapshot: RunSkillSnapshotResponse | None
    input_file_ids: list[str]
    pending_input: RunInputRequestResponse | None = None
    pending_approvals: list[RunPendingToolApprovalResponse] = Field(default_factory=list)
    replayed: bool = False
    retry_warning: str | None = None


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
    status: str
    code: str
    message: str
    data: dict[str, object]
    truncated: bool
    cleanup_error_code: str | None = None


class RunToolCallSummaryResponse(BaseModel):
    id: str
    tool_call_id: str
    ordinal: int
    tool_id: str | None
    tool_kind: str | None
    model_name: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    safe_error_code: str | None
    truncated: bool


class RunToolCallDetailResponse(RunToolCallSummaryResponse):
    arguments: dict[str, object] | None = None
    outcome: RunToolOutcomeResponse | None
    artifacts: list[ArtifactFileRefResponse] = Field(default_factory=list)
    safe_skill_projection: SafeSkillApprovalProjectionResponse | None = None


class RunToolCallListResponse(BaseModel):
    items: list[RunToolCallSummaryResponse]
    next_after_ordinal: int | None


__all__ = [
    "RunAnswerRequest",
    "RunAttemptUsageResponse",
    "RunEventListResponse",
    "RunEventResponse",
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
