"""Exercise remaining lifecycle and orchestration branches."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from application.runs import RunExecutionService, RunQueryService
from workers import CommandConsumer
from application.sessions import SessionNotFoundError
from bootstrap import create_app
from bootstrap.lifespan import lifespan
from domain.events import ApplicationEvent
from domain.runs import CommandType, RunCommand, RunSummary
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
async def test_run_query_and_command_consumer_error_paths() -> None:
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
        def __init__(self): self.claimed = False
        async def claim_next_command(self):
            if self.claimed: return None
            self.claimed = True
            return RunCommand("c", CommandType.RUN_START, "s", "r", {}, NOW)
    class Execution:
        async def execute(self, command): raise RuntimeError("fail")
        async def fail(self, command, error): self.error = error
    consumer = CommandConsumer(Queue(), Execution(), poll_interval=0.001)
    task = asyncio.create_task(consumer.run()); await asyncio.sleep(0.005); consumer._stop.set(); await asyncio.wait_for(task, timeout=1)
