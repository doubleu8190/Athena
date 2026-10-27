"""Exercise remaining lifecycle and orchestration branches."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from application.runs import RunQueryService
from workers import RunConsumer
from application.sessions import SessionNotFoundError
from bootstrap import create_app
from bootstrap.lifespan import lifespan
from domain.runs import CommandType, RunCommand, RunStartPayload, RunSummary
from domain.sessions import Session
from fastapi.testclient import TestClient

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_lifespan_context_manager_resources_and_failed_cleanup() -> None:
    events = []
    class Context:
        async def __aenter__(self): events.append("enter")
        async def __aexit__(self, *args): events.append("exit")
    class App:
        state = SimpleNamespace()
    async with lifespan(App(), [Context()]):
        events.append("body")
    assert events == ["enter", "body", "exit"]
    class Broken:
        async def start(self): raise RuntimeError("broken")
    with pytest.raises(RuntimeError):
        async with lifespan(App(), [Broken()]):
            pass


@pytest.mark.asyncio
async def test_run_query_and_consumer_error_paths() -> None:
    class Sessions:
        async def get(self, key): return Session.create(session_id="s", title="s", now=NOW) if key == "s" else None
    class Runs:
        async def list_for_session(self, key): return []
        async def get(self, key): return RunSummary("r", "s", "running", "t") if key == "r" else None
    service = RunQueryService(Sessions(), Runs())
    assert await service.list_for_session("s") == []
    with pytest.raises(SessionNotFoundError): await service.list_for_session("missing")
    with pytest.raises(LookupError): await service.get("missing")
    class Queue:
        pass
    class Execution:
        async def execute(self, command): raise RuntimeError("fail")
        async def fail(self, command, error): self.error = error
        error = None
    consumer = RunConsumer(Queue(), Execution(), poll_interval=0.001)
    await consumer._execute_run(
        RunCommand(
            "c", CommandType.RUN_START, "s", "r",
            RunStartPayload(message="hello", message_id="m"), NOW
        )
    )
    assert consumer._execution.error["code"] == "run_execution_failed"
