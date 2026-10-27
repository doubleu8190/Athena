"""运行和命令 HTTP DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from application.runs.responses import (
    CommandEnqueueResponse,
    CommandStatusResponse as ApplicationCommandStatusResponse,
    RunCancellationResponse,
)
from application.common.responses import RunTimelineResponse as ApplicationRunTimelineResponse
from interfaces.http.dto.sessions import RunSummaryResponse


class RunListResponse(BaseModel):
    items: list[RunSummaryResponse]
    total: int
    has_more: bool


class SubmitRunRequest(BaseModel):
    session_id: str | None = None
    project_id: str | None = None
    message: str = ""
    command_id: str = Field(min_length=1)
    message_id: str | None = Field(default=None, min_length=1)
    attachment_ids: list[str] = Field(default_factory=list)


class SubmitRunResponse(BaseModel):
    command_id: str
    session_id: str
    run_id: str | None = None
    message_id: str | None = None
    attachment_ids: list[str] = Field(default_factory=list)
    status: str
    deduplicated: bool

    @classmethod
    def from_application(cls, value: CommandEnqueueResponse) -> "SubmitRunResponse":
        return cls(
            command_id=value.command_id,
            session_id=value.session_id,
            run_id=value.run_id,
            message_id=value.message_id,
            attachment_ids=list(value.attachment_ids),
            status=value.status.value if hasattr(value.status, "value") else value.status,
            deduplicated=value.deduplicated,
        )


class CancelCommandResponse(BaseModel):
    command_id: str | None = None
    run_id: str | None = None
    status: str
    already_terminal: bool

    @classmethod
    def from_application(cls, value: RunCancellationResponse) -> "CancelCommandResponse":
        return cls(
            command_id=value.command_id,
            run_id=value.run_id,
            status=value.status,
            already_terminal=value.already_terminal,
        )


class CommandStatusResponse(BaseModel):
    command_id: str
    session_id: str
    run_id: str | None = None
    command_type: str
    status: str
    attempt: int = 0
    result: Any = None
    error: Any = None

    @classmethod
    def from_application(cls, value: ApplicationCommandStatusResponse) -> "CommandStatusResponse":
        return cls(
            command_id=value.command_id,
            session_id=value.session_id,
            run_id=value.run_id,
            command_type=value.command_type.value if hasattr(value.command_type, "value") else value.command_type,
            status=value.status.value if hasattr(value.status, "value") else value.status,
            result=value.result,
            error=value.error,
        )


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
            events=[RunTimelineEventResponse(
                session_seq=event.session_seq,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                run_id=event.run_id,
                payload=event.payload,
            ) for event in value.events],
            next_after_seq=value.next_after_seq,
            has_more=value.has_more,
            links={key: list(items) for key, items in value.links.items()},
        )
