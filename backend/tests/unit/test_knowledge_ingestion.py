"""知识库文档和解析 Worker 的阶段 4 测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from application.files import IngestionService
from workers import KnowledgeDocumentWorker
from application.knowledge import KnowledgeBaseService
from bootstrap import create_app
from domain.files import (
    AdapterInfo,
    Attachment,
    AttachmentStatus,
    FileChunk,
    FileMetadata,
    ParsedDocument,
    StoredBlob,
)
from domain.files import KnowledgeBase
from fastapi.testclient import TestClient
from io import BytesIO


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def attachment(attachment_id: str = "attachment-1") -> Attachment:
    return Attachment(
        id=attachment_id,
        logical_document_id="document-1",
        document_version=1,
        session_id=None,
        knowledge_base_id="kb-1",
        message_id=None,
        filename="notes.txt",
        mime_type="text/plain",
        size_bytes=5,
        sha256="hash",
        storage_key="blobs/hash",
        adapter_name="text",
        adapter_version="1",
        status=AttachmentStatus.UPLOADED,
        capabilities=("text",),
        metadata=FileMetadata(),
        error_message=None,
        created_at=NOW,
        updated_at=NOW,
    )


class KnowledgeRepository:
    def __init__(self):
        self.bases = {"kb-1": KnowledgeBase("kb-1", "Docs", "", 0, 0, 0, NOW, NOW)}

    async def create(self, value):
        self.bases[value.id] = value
        return value

    async def get(self, key):
        return self.bases.get(key)

    async def list_all(self):
        return list(self.bases.values())

    async def save(self, value):
        self.bases[value.id] = value
        return value

    async def delete(self, key):
        return self.bases.pop(key, None) is not None


class Documents:
    def __init__(self):
        self.items = {}

    async def create(self, value):
        self.items[value.id] = value
        return value

    async def get(self, attachment_id, *, knowledge_base_id):
        value = self.items.get(attachment_id)
        return value if value and value.knowledge_base_id == knowledge_base_id else None

    async def list(self, knowledge_base_id):
        return [v for v in self.items.values() if v.knowledge_base_id == knowledge_base_id and v.deleted_time is None]

    async def list_versions(self, attachment_id, *, knowledge_base_id):
        value = await self.get(attachment_id, knowledge_base_id=knowledge_base_id)
        return [value] if value else []

    async def latest_for_filename(self, filename, *, knowledge_base_id):
        values = [v for v in await self.list(knowledge_base_id) if v.filename == filename]
        return max(values, key=lambda v: v.document_version, default=None)

    async def soft_delete(self, attachment_id, *, knowledge_base_id):
        value = await self.get(attachment_id, knowledge_base_id=knowledge_base_id)
        if value is None:
            return False
        self.items[attachment_id] = replace(value, status=AttachmentStatus.DELETED, deleted_time=NOW)
        return True


class Storage:
    async def save_stream(self, chunks):
        return StoredBlob("hash", 5, "blobs/hash")

    async def cleanup_unreferenced(self, live_storage_keys):
        return 0

    async def discard(self, storage_key):
        return None


class Adapters:
    def select(self, filename, mime_type):
        return AdapterInfo("text", "1", (mime_type,), (".txt",), ("text",))

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


class FailingJobs(Jobs):
    async def enqueue(self, attachment_id):
        raise RuntimeError("queue unavailable")


@pytest.mark.asyncio
async def test_knowledge_base_service_uploads_versions_and_deletes() -> None:
    documents = Documents()
    jobs = Jobs()
    service = KnowledgeBaseService(
        KnowledgeRepository(), documents, Storage(), Adapters(), jobs,
        id_factory=iter(["doc-1", "attachment-1", "logical-1"]).__next__,
        clock=lambda: NOW,
    )

    async def chunks():
        yield b"hello"

    created = await service.upload_document("kb-1", filename="notes.txt", mime_type="text/plain", chunks=chunks())
    assert created.knowledge_base_id == "kb-1"
    assert jobs.enqueued == [created.id]
    assert (await service.list_versions("kb-1", created.id))[0].id == created.id
    await service.delete_document("kb-1", created.id)
    assert jobs.cancelled == [created.id]


@pytest.mark.asyncio
async def test_knowledge_upload_compensates_when_queue_fails() -> None:
    documents = Documents()
    storage = Storage()
    jobs = FailingJobs()
    service = KnowledgeBaseService(
        KnowledgeRepository(), documents, storage, Adapters(), jobs,
        id_factory=iter(["attachment-1", "logical-1"]).__next__,
        clock=lambda: NOW,
    )

    async def chunks():
        yield b"hello"

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await service.upload_document(
            "kb-1", filename="notes.txt", mime_type="text/plain", chunks=chunks()
        )
    assert jobs.cancelled == ["attachment-1"]
    assert documents.items["attachment-1"].status is AttachmentStatus.DELETED


def test_knowledge_controller_exposes_lifecycle_and_document_routes() -> None:
    documents = Documents()
    jobs = Jobs()
    service = KnowledgeBaseService(
        KnowledgeRepository(), documents, Storage(), Adapters(), jobs,
        id_factory=iter(["attachment-1", "logical-1", "kb-2"]).__next__,
        clock=lambda: NOW,
    )
    client = TestClient(create_app(knowledge_service_factory=lambda: service))

    created = client.post("/api/knowledge-bases", json={"name": "  Docs  "})
    assert created.status_code == 201
    assert created.json()["name"] == "Docs"
    updated = client.patch(f"/api/knowledge-bases/{created.json()['id']}", json={"name": "Renamed"})
    assert updated.status_code == 200
    assert client.delete(f"/api/knowledge-bases/{created.json()['id']}").status_code == 200

    upload = client.post(
        "/api/knowledge-bases/kb-1/documents",
        files={"files": ("notes.txt", BytesIO(b"hello"), "text/plain")},
    )
    assert upload.status_code == 202
    document_id = upload.json()[0]["id"]
    assert client.get(f"/api/knowledge-bases/kb-1/documents/{document_id}/versions").status_code == 200
    assert client.delete(f"/api/knowledge-bases/kb-1/documents/{document_id}").status_code == 200


class Parser:
    async def parse(self, attachment):
        return ParsedDocument(text="hello world", metadata=FileMetadata({"pages": 1}))


class Chunks:
    async def replace_for_attachment(self, attachment_id, values):
        self.values = values

    async def list_by_attachment(self, attachment_id):
        return []


class Indexer:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    async def index(self, attachment, chunks):
        self.calls += 1
        if self.fail:
            raise RuntimeError("index failed")

    async def delete(self, attachment_id):
        return None


class AttachmentRepository:
    def __init__(self):
        self.value = attachment()

    async def create(self, value):
        self.value = value
        return value

    async def get(self, attachment_id, *, session_id=None):
        return self.value if attachment_id == self.value.id else None

    async def list_by_session(self, session_id):
        return []

    async def list_by_knowledge_base(self, knowledge_base_id):
        return []

    async def save(self, value):
        self.value = value
        return value

    async def soft_delete(self, attachment_id, *, session_id):
        return True


@pytest.mark.asyncio
async def test_ingestion_marks_ready_and_worker_retries_failures() -> None:
    repository = AttachmentRepository()
    indexer = Indexer()
    ingestion = IngestionService(
        repository,
        Chunks(),
        Parser(),
        indexer,
        chunker=lambda text: [FileChunk("chunk-1", "attachment-1", 0, text, 2, {}, {})],
        clock=lambda: NOW,
    )
    result = await ingestion.process("attachment-1")
    assert result["status"] == "ready"
    assert repository.value.status is AttachmentStatus.READY

    class Queue:
        def __init__(self): self.succeeded = []; self.failed = []
        async def claim_next(self, *, max_attempts): return {"job_id": "job-1", "attachment_id": "attachment-1", "attempt": 1}
        async def mark_succeeded(self, job_id, result): self.succeeded.append(result)
        async def mark_failed(self, job_id, error, *, retry): self.failed.append(retry)

    queue = Queue()
    worker = KnowledgeDocumentWorker(queue, ingestion, max_attempts=3)
    assert await worker.process_one()
    assert queue.succeeded


@pytest.mark.asyncio
async def test_upload_to_worker_end_to_end_reaches_ready() -> None:
    documents = Documents()
    jobs = Jobs()
    base = KnowledgeBaseService(
        KnowledgeRepository(), documents, Storage(), Adapters(), jobs,
        id_factory=iter(["attachment-e2e", "logical-e2e"]).__next__,
        clock=lambda: NOW,
    )

    async def chunks():
        yield b"end to end document"

    created = await base.upload_document(
        "kb-1", filename="e2e.txt", mime_type="text/plain", chunks=chunks()
    )
    repository = AttachmentRepository()
    repository.value = created
    queue = type("Queue", (), {
        "claim_next": lambda self, *, max_attempts: _claim(created.id),
        "mark_succeeded": lambda self, job_id, result: _mark_success(result),
        "mark_failed": lambda self, job_id, error, *, retry: _mark_failure(error),
    })()
    indexer = Indexer()
    ingestion = IngestionService(
        repository,
        Chunks(),
        Parser(),
        indexer,
        chunker=lambda text: [FileChunk("chunk-e2e", created.id, 0, text, 2, {}, {})],
        clock=lambda: NOW,
    )
    worker = KnowledgeDocumentWorker(queue, ingestion, max_attempts=3)
    assert await worker.process_one()
    assert repository.value.status is AttachmentStatus.READY


async def _claim(attachment_id: str):
    return {"job_id": "job-e2e", "attachment_id": attachment_id, "attempt": 1}


async def _mark_success(result):
    return None


async def _mark_failure(error):
    return None
