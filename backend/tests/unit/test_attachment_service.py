"""附件 Service 和 Controller 的阶段 4 契约测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from io import BytesIO

from fastapi.testclient import TestClient

from application.files import AttachmentService
from bootstrap import create_app
from domain.files import (
    AdapterInfo,
    Attachment,
    AttachmentStatus,
    FileMetadata,
    StoredBlob,
)
from domain.sessions import Session


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Sessions:
    async def get(self, session_id):
        return Session.create(session_id=session_id, title="Chat", now=NOW) if session_id == "session-1" else None


class Attachments:
    def __init__(self):
        self.items = {}

    async def create(self, value):
        self.items[value.id] = value
        return value

    async def get(self, attachment_id, *, session_id=None):
        value = self.items.get(attachment_id)
        return value if value and value.session_id == session_id and value.deleted_time is None else None

    async def list_by_session(self, session_id):
        return [value for value in self.items.values() if value.session_id == session_id and value.deleted_time is None]

    async def list_by_knowledge_base(self, knowledge_base_id):
        return []

    async def save(self, value):
        self.items[value.id] = value
        return value

    async def soft_delete(self, attachment_id, *, session_id):
        value = await self.get(attachment_id, session_id=session_id)
        if value is None:
            return False
        self.items[attachment_id] = replace(
            value, status=AttachmentStatus.DELETED, deleted_time=NOW
        )
        return True


class Storage:
    def __init__(self):
        self.discarded = []

    async def save_stream(self, chunks):
        data = b""
        async for chunk in chunks:
            data += chunk
        return StoredBlob("hash", len(data), "blobs/hash")

    async def cleanup_unreferenced(self, live_storage_keys):
        return 0

    async def discard(self, storage_key):
        self.discarded.append(storage_key)


class Adapters:
    def select(self, filename, mime_type):
        if not filename.endswith(".txt"):
            raise ValueError("unsupported file")
        return AdapterInfo("text", "1", ("text/plain",), (".txt",), ("text",))

    def supported_extensions(self):
        return [".txt"]


class Jobs:
    def __init__(self):
        self.enqueued = []
        self.cancelled = []

    async def enqueue(self, attachment_id):
        self.enqueued.append(attachment_id)
        return True

    async def cancel(self, attachment_id):
        self.cancelled.append(attachment_id)


def make_service():
    attachments = Attachments()
    storage = Storage()
    jobs = Jobs()
    service = AttachmentService(
        Sessions(), attachments, storage, Adapters(), jobs,
        id_factory=iter(["attachment-1", "document-1"]).__next__,
        clock=lambda: NOW,
    )
    return service, attachments, storage, jobs


def test_attachment_service_uploads_enqueues_and_deletes() -> None:
    service, attachments, storage, jobs = make_service()

    async def chunks():
        yield b"hello"

    import asyncio
    created = asyncio.run(service.upload("session-1", filename="notes.txt", mime_type="text/plain", chunks=chunks()))
    assert created.status is AttachmentStatus.UPLOADED
    assert jobs.enqueued == ["attachment-1"]
    asyncio.run(service.delete("session-1", created.id))
    assert jobs.cancelled == [created.id]
    assert attachments.items[created.id].status is AttachmentStatus.DELETED


def test_attachment_controller_exposes_upload_list_detail_delete() -> None:
    service, _, _, _ = make_service()
    client = TestClient(create_app(attachment_service_factory=lambda: service))

    upload = client.post(
        "/api/sessions/session-1/attachments",
        files={"files": ("notes.txt", BytesIO(b"hello"), "text/plain")},
    )
    assert upload.status_code == 202
    attachment_id = upload.json()[0]["id"]
    assert client.get("/api/sessions/session-1/attachments").json()[0]["id"] == attachment_id
    assert client.get(f"/api/sessions/session-1/attachments/{attachment_id}").status_code == 200
    assert client.get("/api/sessions/session-1/attachment-types").json() == {"extensions": [".txt"]}
    assert client.delete(f"/api/sessions/session-1/attachments/{attachment_id}").json() == {
        "status": "deleted", "file_id": attachment_id
    }
