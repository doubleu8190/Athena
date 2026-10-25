"""运行请求准备服务的 application 用例测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from application.runs import RunRequestPreparationService
from application.runs.request_contracts import RootKnowledgeBase, RootRequestState
from application.sessions import SessionNotFoundError
from domain.files import Attachment, AttachmentStatus, FileMetadata
from domain.knowledge import KnowledgeBase
from domain.sessions import Message, MessageRole, Session


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def request_state(**values: object) -> RootRequestState:
    return RootRequestState.model_validate(values)


class Sessions:
    async def get(self, session_id: str):
        return Session.create(session_id=session_id, title="Chat", now=NOW) if session_id == "session-1" else None


class Messages:
    def __init__(self, values: list[Message] | None = None) -> None:
        self.values = {value.id: value for value in values or []}
        self.saved: list[Message] = []

    async def get(self, message_id: str):
        return self.values.get(message_id)

    async def save(self, message: Message) -> str:
        self.values[message.id] = message
        self.saved.append(message)
        return message.id

    async def list_recent_conversation(self, session_id: str, *, limit: int | None = None):
        values = [value for value in self.values.values() if value.session_id == session_id]
        return values[:limit] if limit is not None else values


class Attachments:
    def __init__(self, values: list[Attachment] | None = None) -> None:
        self.values = {value.id: value for value in values or []}

    async def get(self, attachment_id: str, *, session_id: str | None = None):
        value = self.values.get(attachment_id)
        if value is None or value.deleted_time is not None:
            return None
        return value if value.session_id == session_id else None


class KnowledgeBases:
    async def list_knowledge_bases(self):
        return [KnowledgeBase("kb-1", "Docs", "reference", 4, 3, 100, NOW, NOW)]


def attachment(
    attachment_id: str = "attachment-1",
    *,
    session_id: str | None = "session-1",
    status: AttachmentStatus = AttachmentStatus.READY,
) -> Attachment:
    return Attachment(
        attachment_id,
        "document-1",
        1,
        session_id,
        None,
        None,
        "notes.txt",
        "text/plain",
        12,
        "hash",
        "storage/notes.txt",
        "text",
        "1",
        status,
        ("text",),
        FileMetadata(),
        None,
        NOW,
        NOW,
    )


def make_service(
    *,
    messages: Messages | None = None,
    attachments: Attachments | None = None,
) -> tuple[RunRequestPreparationService, Messages, Attachments]:
    message_store = messages or Messages()
    attachment_store = attachments or Attachments([attachment()])
    service = RunRequestPreparationService(
        Sessions(),
        message_store,
        attachment_store,
        KnowledgeBases(),
        id_factory=lambda: "generated-message",
        clock=lambda: NOW,
    )
    return service, message_store, attachment_store


@pytest.mark.asyncio
async def test_prepare_rejects_non_ready_attachment() -> None:
    service, _, _ = make_service(
        attachments=Attachments([attachment(status=AttachmentStatus.PROCESSING)])
    )

    with pytest.raises(ValueError, match="attachment_not_ready"):
        await service.prepare(
            request=request_state(
                session_id="session-1",
                run_id="run-1",
                user_message="use file",
                attachment_ids=["attachment-1"],
            )
        )


@pytest.mark.asyncio
async def test_prepare_rejects_attachment_from_another_session() -> None:
    service, _, _ = make_service(
        attachments=Attachments([attachment(session_id="session-2")])
    )

    with pytest.raises(ValueError, match="attachment_not_found"):
        await service.prepare(
            request=request_state(
                session_id="session-1",
                run_id="run-1",
                user_message="use file",
                attachment_ids=["attachment-1"],
            )
        )


@pytest.mark.asyncio
async def test_prepare_persists_message_idempotently_and_loads_references() -> None:
    service, messages, _ = make_service()
    request = request_state(
        session_id="session-1",
        run_id="run-1",
        message_id="message-1",
        user_message="summarize",
        attachment_ids=["attachment-1"],
    )

    first = await service.prepare(request=request)
    second = await service.prepare(request=request)

    assert first == second
    assert len(messages.saved) == 1
    assert messages.saved[0].role is MessageRole.USER
    assert first.attachment_refs[0].status == "ready"


@pytest.mark.asyncio
async def test_load_history_excludes_current_message() -> None:
    values = [
        Message("old", "session-1", MessageRole.USER, "old", NOW),
        Message("current", "session-1", MessageRole.USER, "current", NOW),
    ]
    service, _, _ = make_service(messages=Messages(values))

    history = await service.load_history(
        session_id="session-1", exclude_message_id="current"
    )

    assert [item["message_id"] for item in history] == ["old"]


@pytest.mark.asyncio
async def test_loaders_return_attachment_and_knowledge_metadata() -> None:
    service, _, _ = make_service()

    attachments = await service.load_attachments(
        session_id="session-1", attachment_ids=["attachment-1", "missing"]
    )
    knowledge_bases = await service.load_knowledge_bases()

    assert attachments == [
        {
            "id": "attachment-1",
            "filename": "notes.txt",
            "mime_type": "text/plain",
            "size_bytes": 12,
            "status": "ready",
        }
    ]
    assert knowledge_bases == [
        RootKnowledgeBase(
            id="kb-1",
            name="Docs",
            description="reference",
            document_count=4,
            ready_document_count=3,
        )
    ]


@pytest.mark.asyncio
async def test_prepare_rejects_unknown_session() -> None:
    service, _, _ = make_service()

    with pytest.raises(SessionNotFoundError):
        await service.prepare(
            request=request_state(session_id="missing", user_message="hello")
        )
