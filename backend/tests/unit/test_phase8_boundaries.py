"""Boundary matrix for migrated ports and HTTP error mappings."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from application.runs import (
    CommandRejectedError, RunCommandService, RunExecutionService,
)
from application.sessions import SessionNotFoundError as SessionMissing
from bootstrap import create_app
from domain.events import ApplicationEvent
from domain.runs import (
    CommandExecutionResult, CommandStatus, CommandStatusRecord, CommandType,
    RunCommand, RunStatus, RunSummary,
)
from domain.sessions import Session
from bootstrap.check import check_app
from application.approval import ApprovalService
from application.files import AttachmentNotFoundError, SessionNotFoundError
from application.knowledge import KnowledgeBaseNotFoundError, KnowledgeDocumentNotFoundError

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class SessionRepo:
    async def get(self, key): return Session.create(session_id=key, title="x", now=NOW) if key == "s" else None


class RunRepo:
    async def get(self, key): return RunSummary(key, "s", "running", "thread") if key == "r" else None
    async def get_active_for_session(self, key): return await self.get("r") if key == "s" else None


class CommandRepo:
    def __init__(self, *, result=True): self.result = result; self.values = {}
    async def enqueue_command(self, value):
        if isinstance(self.result, Exception): raise self.result
        if isinstance(self.result, str): raise ValueError(self.result)
        self.values[value.command_id] = CommandStatusRecord(value.command_id, value.session_id, value.run_id, value.command_type, CommandStatus.QUEUED)
        return self.result
    async def get_command(self, key): return self.values.get(key)


def make_run_service(commands=None, sessions=None):
    return RunCommandService(sessions or SessionRepo(), commands or CommandRepo(), RunRepo(), id_factory=iter(["m", "r", "c", "c2"]).__next__, clock=lambda: NOW)


@pytest.mark.asyncio
async def test_run_command_service_validation_bool_and_cancel_paths() -> None:
    with pytest.raises(SessionMissing): await make_run_service().submit("missing", command_id="c", message="x")
    with pytest.raises(ValueError): await make_run_service().submit("s", command_id=" ", message="x")
    commands = CommandRepo(result=True)
    service = make_run_service(commands)
    accepted = await service.submit("s", command_id="c", message="x")
    assert accepted.deduplicated is False
    commands = CommandRepo(result=False)
    service = make_run_service(commands)
    accepted = await service.submit("s", command_id="c", message="x")
    assert accepted.deduplicated is True
    commands = CommandRepo(result="session_busy")
    with pytest.raises(CommandRejectedError): await make_run_service(commands).submit("s", command_id="c", message="x")
    accepted = await make_run_service().cancel_session("s")
    assert accepted.status is CommandStatus.QUEUED


@pytest.mark.asyncio
async def test_run_execution_cancel_missing_id_and_error_result_paths() -> None:
    class Lifecycle:
        async def update_status(self, *a): pass
        async def complete(self, *a): pass
        async def fail(self, *a): pass
        async def cancel(self, *a): pass
    class Queue:
        def __init__(self): self.done=[]
        async def complete_command(self, *a, **kw): self.done.append((a,kw))
    class Executor:
        async def cancel(self, value): pass
        async def execute(self, value): return CommandExecutionResult(error="bad")
    class Events:
        async def publish(self, value): return value
    queue = Queue(); service = RunExecutionService(Lifecycle(), queue, Executor(), Events())
    await service.execute(RunCommand("c", CommandType.RUN_CANCEL, "s", None, {}, NOW))
    await service.execute(RunCommand("c2", CommandType.RUN_START, "s", None, {}, NOW))
    assert len(queue.done) == 2


def test_bootstrap_probe_and_unconfigured_query_routes() -> None:
    check_app()
    from fastapi import FastAPI
    from  interfaces.http.controllers.query_controller import build_query_router
    app = FastAPI()
    app.include_router(build_query_router())
    client = TestClient(app)
    for path, method in [
        ("/tools", "get"), ("/providers", "get"), ("/settings", "get"),
        ("/knowledge-bases", "get"), ("/memory", "get"), ("/retrieval/runs", "get"),
        ("/retrieval/runs/r", "get"), ("/memory/m", "get"), ("/memory/m/revisions", "get"),
    ]:
        assert getattr(client, method)(path).status_code == 503
    assert client.post("/memory/search", json={"query": "q"}).status_code == 503


def test_controller_error_mappings_for_approval_tools_files_and_knowledge() -> None:
    class Approval:
        async def pending(self, session_id=None): raise RuntimeError("x")
        async def history(self, *args, **kwargs): return []
        async def resolve(self, *args, **kwargs): return False
        async def resolve_batch(self, *args, **kwargs): return False
    class Tool:
        async def list(self): return []
        async def update_governance(self, *args, **kwargs): raise ValueError("bad risk")
    class Files:
        async def list_for_session(self, key): raise SessionNotFoundError(key)
        async def supported_extensions(self, key): raise SessionNotFoundError(key)
        async def get(self, *args): raise AttachmentNotFoundError("a")
        async def delete(self, *args): raise AttachmentNotFoundError("a")
    class Knowledge:
        async def list(self): return []
        async def supported_extensions(self): return []
        async def update(self, *args): raise KnowledgeBaseNotFoundError("k")
        async def delete(self, *args): raise KnowledgeBaseNotFoundError("k")
        async def list_documents(self, *args): raise KnowledgeBaseNotFoundError("k")
        async def list_versions(self, *args): raise KnowledgeDocumentNotFoundError("a")
        async def delete_document(self, *args): raise KnowledgeDocumentNotFoundError("a")
    from  interfaces.http.controllers.files_controller import build_files_router
    client = TestClient(create_app(
        approval_service_factory=lambda: Approval(),
        tool_service_factory=lambda: Tool(),
        attachment_service_factory=lambda: Files(),
        knowledge_service_factory=lambda: Knowledge(),
    ))
    assert client.get("/api/sessions/s/attachments").status_code == 404
    assert client.get("/api/sessions/s/attachment-types").status_code == 404
    assert client.get("/api/sessions/s/attachments/a").status_code == 404
    assert client.delete("/api/sessions/s/attachments/a").status_code == 404
    assert client.patch("/api/tools/x", json={"risk_level": "high"}).status_code == 400
    assert client.patch("/api/tools/x", json={}).status_code == 400
    assert client.patch("/api/knowledge-bases/k", json={"name": "x"}).status_code == 404
    assert client.delete("/api/knowledge-bases/k").status_code == 404
    assert client.get("/api/knowledge-bases/k/documents").status_code == 404
    assert client.get("/api/knowledge-bases/k/documents/a/versions").status_code == 404
    assert client.delete("/api/knowledge-bases/k/documents/a").status_code == 404

