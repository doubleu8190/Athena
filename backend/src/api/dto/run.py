"""Run HTTP 请求与响应 DTO。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RunSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=32_000)


class RunAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=32_000)


class RunRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    confirm_unknown: bool = False


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
    usage: RunUsageResponse
    pending_input: RunInputRequestResponse | None = None
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
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    safe_error_code: str | None
    truncated: bool


class RunToolCallDetailResponse(RunToolCallSummaryResponse):
    arguments: dict[str, object]
    outcome: RunToolOutcomeResponse | None


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
    "RunResponse",
    "RunRetryRequest",
    "RunSubmitRequest",
    "RunToolCallDetailResponse",
    "RunToolCallListResponse",
    "RunToolCallSummaryResponse",
    "RunToolOutcomeResponse",
    "RunUsageResponse",
]
