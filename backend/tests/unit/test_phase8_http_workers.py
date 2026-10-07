"""HTTP boundary and worker lifecycle coverage for the final cutover."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from application.events import EventStreamService
from application.files import IngestionService
from application.memory import MemoryWriteWorkflow
from application.orchestration import OrchestrationService, WorkerSchedulerService
from application.runs import RunCommandService, RunNotFoundError, CommandNotFoundError
from workers import KnowledgeDocumentWorker, MemoryWorker
from application.sessions import SessionNotFoundError
from application.approval import ApprovalService
from bootstrap import create_app
from domain.events import ApplicationEvent, EventDurability, EventType
from domain.files import Attachment, AttachmentStatus, FileChunk, FileLocator, FileMetadata, ParsedDocument
from domain.memory import CompletedTurn, MemoryCandidate, MemoryResolution, ResolutionAction
from domain.orchestration import Plan, Task, TaskExecution, TaskResult, TaskStatus
from domain.sessions import Session
from domain.approval import ApprovalDecision, ApprovalRequest
from domain.runs import CommandEnqueueResult, CommandStatus, CommandStatusRecord, CommandType, RunSummary

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _attachment() -> Attachment:
    return Attachment("a", "d", 1, "s", None, None, "x.txt", "text/plain", 1, "h", "k", "text", "1", AttachmentStatus.UPLOADED, (), FileMetadata(), None, NOW, NOW)


def test_file_and_knowledge_controller_error_mappings() -> None:
    class Files:
        async def list_for_session(self, key): return []
        async def supported_extensions(self, key): return [".txt"]
        async def get(self, *args):
            from  application.files import AttachmentNotFoundError
            raise AttachmentNotFoundError("a")
        async def upload(self, *args, **kwargs): raise ValueError("unsupported")
        async def delete(self, *args): return None
    class Knowledge:
        async def list(self): return []
        async def supported_extensions(self): return [".txt"]
        async def create(self, *args): raise ValueError("bad")
        async def update(self, *args): return SimpleNamespace(id="k", name="k", description="", document_count=0, ready_document_count=0, total_size_bytes=0, created_at=NOW, updated_at=NOW)
        async def delete(self, *args): return None
        async def list_documents(self, *args): return []
        async def upload_document(self, *args, **kwargs): raise ValueError("unsupported")
        async def list_versions(self, *args): return []
        async def delete_document(self, *args): return None
    client = TestClient(create_app(attachment_service_factory=lambda: Files(), knowledge_service_factory=lambda: Knowledge()))
    assert client.get("/api/sessions/s/attachments").status_code == 200
    assert client.get("/api/sessions/s/attachment-types").json() == {"extensions": [".txt"]}
    assert client.get("/api/sessions/s/attachments/a").status_code == 404
    assert client.post("/api/sessions/s/attachments", files={"files": ("x.txt", BytesIO(b"x"), "text/plain")}).status_code == 400
    assert client.get("/api/knowledge-bases").status_code == 200
    assert client.post("/api/knowledge-bases", json={"name": ""}).status_code == 422
    assert client.post("/api/knowledge-bases", json={"name": "x"}).status_code == 400
    assert client.patch("/api/knowledge-bases/k", json={"name": "x"}).status_code == 200
    assert client.post("/api/knowledge-bases/k/documents", files={"files": ("x.txt", BytesIO(b"x"), "text/plain")}).status_code == 400
    assert client.get("/api/knowledge-bases/k/documents").status_code == 200
    assert client.get("/api/knowledge-bases/k/documents/a/versions").status_code == 200
    assert client.delete("/api/knowledge-bases/k/documents/a").status_code == 200


@pytest.mark.asyncio
async def test_document_worker_and_memory_worker_success_failure_paths() -> None:
    class Queue:
        def __init__(self, job): self.job = job; self.calls = []
        async def claim_next(self, **kwargs): job, self.job = self.job, None; return job
        async def mark_succeeded(self, *args): self.calls.append(("ok", args))
        async def mark_failed(self, *args, **kwargs): self.calls.append(("fail", args, kwargs))
    class Ingest:
        async def process(self, key): return {"attachment_id": key}
    queue = Queue({"job_id": "j", "attachment_id": "a", "attempt": 1})
    worker = KnowledgeDocumentWorker(queue, Ingest(), max_attempts=2)
    assert await worker.process_one() is True and queue.calls[0][0] == "ok"
    assert await worker.process_one() is False
    class Bad:
        async def process(self, key): raise RuntimeError("bad")
    queue = Queue({"job_id": "j", "attachment_id": "a", "attempt": 2})
    assert await KnowledgeDocumentWorker(queue, Bad(), max_attempts=2).process_one()
    assert queue.calls[0][0] == "fail" and queue.calls[0][2]["retry"] is False

    class Jobs:
        def __init__(self): self.items = [{"turn_id": "t", "session_id": "s"}]; self.calls=[]
        async def recover(self): self.calls.append("recover")
        async def claim(self):
            if self.items: return self.items.pop(0)
            return None
        async def succeed(self, key): self.calls.append(("ok", key))
        async def fail(self, *args, **kwargs): self.calls.append(("fail", args))
    class Workflow:
        async def process(self, turn): return 1
    jobs = Jobs(); memory_worker = MemoryWorker(jobs, Workflow(), poll_interval=0.001)
    task = asyncio.create_task(memory_worker.run()); await asyncio.sleep(0.01); memory_worker.stop(); await task
    assert jobs.calls[0] == "recover" and ("ok", "t") in jobs.calls


@pytest.mark.asyncio
async def test_memory_workflow_create_update_and_skip_paths() -> None:
    candidate = MemoryCandidate("fact", "t", "preference", "general", 0.9)
    class Trigger:
        def __init__(self, value): self.value=value
        def should_process(self, turn): return self.value
    class Extractor:
        async def extract(self, turn): return [candidate]
    class Resolver:
        def __init__(self, action): self.action=action
        async def resolve(self, values): return [MemoryResolution(self.action, candidate, "m-1", "updated")]
    class Memory:
        def __init__(self): self.calls=[]
        async def add(self, command): self.calls.append(("add", command)); return "m"
        async def revise(self, *args, **kwargs): self.calls.append(("revise", args)); return "m2"
    turn = CompletedTurn("t", "s", "u", "a")
    memory = Memory()
    assert await MemoryWriteWorkflow(memory, Trigger(False), Extractor(), Resolver(ResolutionAction.CREATE)).process(turn) == 0
    assert await MemoryWriteWorkflow(memory, Trigger(True), Extractor(), Resolver(ResolutionAction.CREATE)).process(turn) == 1
    assert await MemoryWriteWorkflow(memory, Trigger(True), Extractor(), Resolver(ResolutionAction.UPDATE)).process(turn) == 1
    assert await MemoryWriteWorkflow(memory, Trigger(True), Extractor(), Resolver(ResolutionAction.IGNORE)).process(turn) == 0
    assert [item[0] for item in memory.calls] == ["add", "revise"]


@pytest.mark.asyncio
async def test_orchestration_service_and_scheduler_error_paths() -> None:
    class Repo:
        async def get(self, key): return None
        async def create(self, plan, tasks): return plan
    service = OrchestrationService(Repo())
    with pytest.raises(Exception): await service.get_plan("missing")
    with pytest.raises(RuntimeError): await service.start_ready_tasks("missing")
    class Worker:
        async def cancel(self, run): return 2
        async def execute(self, execution): raise RuntimeError("x")
    class SchedulerRepo:
        async def get(self, key): return None
        async def claim_ready_tasks(self, *args): return []
    scheduler = WorkerSchedulerService(SchedulerRepo(), Worker())
    with pytest.raises(ValueError): await scheduler.start_ready_tasks("missing")
    assert await scheduler.cancel_run("r") == 2


def test_run_controller_maps_all_command_errors() -> None:
    class Sessions:
        async def get(self, key): return Session.create(session_id="s", title="s", now=NOW) if key == "s" else None
    class Runs:
        async def get(self, key): return RunSummary("r", "s", "running", "t") if key == "r" else None
        async def get_active_for_session(self, key): return None
    class Commands:
        async def enqueue_command(self, command): raise ValueError("session_busy")
        async def get_command(self, key): return None
    service = RunCommandService(Sessions(), Commands(), Runs(), id_factory=lambda: "id", clock=lambda: NOW)
    client = TestClient(create_app(run_command_factory=lambda: service))
    assert client.post("/api/sessions/s/runs", json={"command_id": "c", "message": "x"}).status_code == 409
    assert client.post("/api/sessions/missing/cancel").status_code == 404
    assert client.post("/api/runs/missing/cancel").status_code == 404
    assert client.get("/api/commands/missing").status_code == 404


def test_approval_controller_maps_conflicts_and_serializes_history() -> None:
    request = ApprovalRequest("a", "s", "r", "c", "tool", {"x": 1}, "low", created_at=NOW)
    class Approval:
        async def pending(self, session_id=None): return [request]
        async def history(self, *args, **kwargs): return [request]
        async def resolve(self, *args, **kwargs): return False
        async def resolve_batch(self, *args, **kwargs): return False
    client = TestClient(create_app(approval_service_factory=lambda: Approval()))
    assert client.get("/api/approvals").json()[0]["approval_id"] == "a"
    assert client.get("/api/approvals/logs").json()[0]["tool_name"] == "tool"
    assert client.post("/api/approvals/a/resolve", json={"decision": "approved", "run_id": "r", "task_id": "t"}).status_code == 409
    assert client.post("/api/approvals/batches/b/resolve", json={"decisions": {}, "run_id": "r"}).status_code == 409


@pytest.mark.asyncio
async def test_sse_heartbeat_and_untagged_event_paths() -> None:
    from  interfaces.http.sse.events import build_events_router
    class Sessions:
        async def get(self, key): return Session.create(session_id=key, title="s", now=NOW)
    class Events:
        async def open_subscription(self, key, after): return asyncio.Queue(), after
        async def replay(self, *args): return []
        async def close(self, *args): self.closed = True
    events = Events(); service = SimpleNamespace(open=events.open_subscription, replay=events.replay, close=events.close)
    app = FastAPI(); app.include_router(build_events_router(lambda: service))
    disconnected = iter([False, True])
    request = Request({"type": "http", "method": "GET", "path": "/sessions/s/events", "headers": [], "app": app})
    async def is_disconnected(): return next(disconnected)
    request.is_disconnected = is_disconnected  # type: ignore[method-assign]
    response = await app.routes[-1].endpoint("s", request, 0, service)
    chunks = [item async for item in response.body_iterator]
    assert chunks == [": heartbeat\n\n"]
    assert events.closed


@pytest.mark.asyncio
async def test_attachment_compensation_and_ingestion_failure_paths() -> None:
    from  application.files import AttachmentService
    from  domain.files import AdapterInfo, StoredBlob
    class Sessions:
        async def get(self, key): return Session.create(session_id=key, title="s", now=NOW)
    class Attachments:
        async def create(self, value): raise RuntimeError("db")
        async def soft_delete(self, *args, **kwargs): self.deleted = True; return True
        async def get(self, *args, **kwargs): return None
    class Storage:
        def __init__(self): self.discarded = False
        async def save_stream(self, chunks): return StoredBlob("h", 1, "k")
        async def discard(self, key): self.discarded = True
    class Adapters:
        def select(self, *args): return AdapterInfo("text", "1", (), (".txt",), ())
        def supported_extensions(self): return []
    class Jobs:
        def __init__(self): self.cancelled = False
        async def enqueue(self, key): pass
        async def cancel(self, key): self.cancelled = True
    storage, jobs = Storage(), Jobs()
    service = AttachmentService(Sessions(), Attachments(), storage, Adapters(), jobs, id_factory=iter(["a", "d"]).__next__, clock=lambda: NOW)
    async def chunks(): yield b"x"
    with pytest.raises(RuntimeError): await service.upload("s", filename="x.txt", mime_type="text/plain", chunks=chunks())
    assert storage.discarded and jobs.cancelled
    with pytest.raises(Exception): await service.get("s", "missing")
