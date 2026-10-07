"""运行和命令 HTTP DTO。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from domain.runs import (
    CommandEnqueueResult,
    CommandStatusRecord,
    CommandType,
)


class SubmitRunRequest(BaseModel):
    message: str = ""
    command_id: str = Field(min_length=1)
    attachment_ids: list[str] = Field(default_factory=list)


class SubmitRunResponse(BaseModel):
    command_id: str
    run_id: str | None = None
    message_id: str | None = None
    attachment_ids: list[str] = Field(default_factory=list)
    status: str
    deduplicated: bool

    @classmethod
    def from_domain(cls, value: CommandEnqueueResult) -> "SubmitRunResponse":
        return cls(
            command_id=value.command_id,
            run_id=value.run_id,
            message_id=value.message_id,
            attachment_ids=list(value.attachment_ids),
            status=str(value.status),
            deduplicated=value.deduplicated,
        )


class CancelCommandResponse(BaseModel):
    command_id: str
    run_id: str | None = None
    status: str

    @classmethod
    def from_domain(cls, value: CommandEnqueueResult) -> "CancelCommandResponse":
        return cls(
            command_id=value.command_id,
            run_id=value.run_id,
            status=str(value.status),
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
    def from_domain(cls, value: CommandStatusRecord) -> "CommandStatusResponse":
        return cls(
            command_id=value.command_id,
            session_id=value.session_id,
            run_id=value.run_id,
            command_type=str(value.command_type),
            status=str(value.status),
            result=value.result,
            error=value.error,
        )
