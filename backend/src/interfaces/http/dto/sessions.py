"""会话 HTTP DTO。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from application.sessions.responses import (
    MessageResponse as ApplicationMessageResponse,
    RunSummaryResponse as ApplicationRunSummaryResponse,
    SessionResponse as ApplicationSessionResponse,
)


class CreateSessionRequest(BaseModel):
    title: str = "New Session"


class UpdateSessionRequest(BaseModel):
    title: str


class SessionMemorySettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    use_memories: bool
    generate_memories: bool
    project_id: str | None


class SessionMemorySettingsResponse(BaseModel):
    session_id: str
    use_memories: bool
    generate_memories: bool
    project_id: str | None


class SessionResponse(BaseModel):
    id: str
    title: str
    status: str
    run_id: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_application(cls, session: ApplicationSessionResponse) -> "SessionResponse":
        return cls(
            id=session.id,
            title=session.title,
            status=session.status,
            run_id=session.run_id,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )


class SessionDeletionResponse(BaseModel):
    status: str
    session_id: str


class ToolCallResponse(BaseModel):
    id: str
    name: str
    args: dict[str, object]


class AttachmentRefResponse(BaseModel):
    id: str
    filename: str
    mime_type: str
    size_bytes: int
    status: str


class MessageResponse(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    timestamp: datetime
    tool_calls: list[ToolCallResponse]
    tool_call_id: str | None
    run_id: str | None
    tool_call_record_id: str | None
    tool_name: str | None
    message_type: str | None
    attachments: list[AttachmentRefResponse]

    @classmethod
    def from_application(cls, message: ApplicationMessageResponse) -> "MessageResponse":
        return cls(
            id=message.id,
            session_id=message.session_id,
            role=message.role,
            content=message.content,
            timestamp=message.timestamp,
            tool_calls=[ToolCallResponse(id=item.id, name=item.name, args=item.args) for item in message.tool_calls],
            tool_call_id=message.tool_call_id,
            run_id=message.run_id,
            tool_call_record_id=message.tool_call_record_id,
            tool_name=message.tool_name,
            message_type=message.message_type,
            attachments=[
                AttachmentRefResponse(
                    id=item.id,
                    filename=item.filename,
                    mime_type=item.mime_type,
                    size_bytes=item.size_bytes,
                    status=item.status,
                )
                for item in message.attachments
            ],
        )


class RunSummaryResponse(BaseModel):
    run_id: str
    session_id: str
    status: str
    root_thread_id: str
    error: object = None
    created_at: datetime | str | None = None
    updated_at: datetime | str | None = None
    started_at: datetime | str | None = None
    title: str = ""

    @classmethod
    def from_application(cls, run: ApplicationRunSummaryResponse) -> "RunSummaryResponse":
        return cls(
            run_id=run.run_id,
            session_id=run.session_id,
            status=run.status,
            root_thread_id=run.root_thread_id,
            error=run.error,
            created_at=run.created_at,
            updated_at=run.updated_at,
            started_at=run.started_at,
            title=run.title,
        )
