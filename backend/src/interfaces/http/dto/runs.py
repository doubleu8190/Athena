"""Run HTTP request and response DTOs."""

from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field

from application.common.responses import RunTimelineResponse as ApplicationRunTimelineResponse
from application.observability import TraceSpan, TraceSpanAttributes
from interfaces.http.dto.sessions import RunSummaryResponse


class RunListResponse(BaseModel):
    items: list[RunSummaryResponse]
    total: int
    has_more: bool


class SubmitRunRequest(BaseModel):
    session_id: str | None = None
    project_id: str | None = None
    message: str = ""
    attachment_ids: list[str] = Field(default_factory=list)


class SubmitRunResponse(BaseModel):
    session_id: str
    message_id: str
    run_id: str
    sequence: int
    status: str

    @classmethod
    def from_application(cls, value) -> "SubmitRunResponse":
        return cls(
            session_id=value.session_id,
            message_id=value.message_id,
            run_id=value.run_id,
            sequence=value.sequence,
            status=value.status,
        )


class StopRunRequest(BaseModel):
    expected_run_id: str | None = None
    displayed_session_seq: int = Field(ge=0)


class StopRunResponse(BaseModel):
    run_id: str | None
    status: str
    no_op: bool

    @classmethod
    def from_application(cls, value) -> "StopRunResponse":
        return cls(run_id=value.run_id, status=value.status, no_op=value.no_op)


class RunTimelineEventResponse(BaseModel):
    session_seq: int
    event_type: str
    occurred_at: datetime | str
    run_id: str | None
    payload: dict[str, object]


class RunTimelineResponse(BaseModel):
    run: RunSummaryResponse
    events: list[RunTimelineEventResponse]
    next_after_seq: int
    has_more: bool
    links: dict[str, list[str]]

    @classmethod
    def from_application(cls, value: ApplicationRunTimelineResponse) -> "RunTimelineResponse":
        return cls(
            run=RunSummaryResponse.from_application(value.run),
            events=[
                RunTimelineEventResponse(
                    session_seq=event.session_seq,
                    event_type=event.event_type,
                    occurred_at=event.occurred_at,
                    run_id=event.run_id,
                    payload=event.payload,
                )
                for event in value.events
            ],
            next_after_seq=value.next_after_seq,
            has_more=value.has_more,
            links={key: list(items) for key, items in value.links.items()},
        )


class RunTraceSpanResponse(BaseModel):
    """链路页面可读取的后端 Trace span。"""

    span_id: str
    parent_span_id: str | None
    span_type: str
    name: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    duration_ms: int | None
    attributes: TraceSpanAttributes
    error: dict[str, str] | None
    sequence: int

    @classmethod
    def from_application(cls, value: TraceSpan) -> "RunTraceSpanResponse":
        return cls(
            span_id=value.span_id,
            parent_span_id=value.parent_span_id,
            span_type=value.span_type,
            name=value.name,
            status=value.status,
            started_at=value.started_at,
            completed_at=value.completed_at,
            duration_ms=value.duration_ms,
            attributes=value.attributes,
            error=value.error,
            sequence=value.sequence,
        )


class RunTraceResponse(BaseModel):
    """完成态 Run 的不可变 span 树响应。"""

    run_id: str
    status: str
    spans: list[RunTraceSpanResponse]
