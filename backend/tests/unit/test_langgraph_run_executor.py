from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from domain.runs import CommandType, RunCommand
from infrastructure.orchestration.langgraph import LangGraphRunExecutor


def _command(run_id: str = "run-1") -> RunCommand:
    return RunCommand(
        command_id=f"command-{run_id}",
        command_type=CommandType.RUN_START,
        session_id="session-1",
        run_id=run_id,
        payload={"message": "hello"},
        issued_at=datetime.now(timezone.utc),
    )


class _BlockingGraph:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def invoke(self, **kwargs):
        self.started.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_cancel_interrupts_active_graph_invocation() -> None:
    graph = _BlockingGraph()
    executor = LangGraphRunExecutor(graph)
    execution = asyncio.create_task(executor.execute(_command()))
    await graph.started.wait()

    await executor.cancel("run-1")

    with pytest.raises(asyncio.CancelledError):
        await execution


@pytest.mark.asyncio
async def test_cancel_unknown_run_is_idempotent() -> None:
    executor = LangGraphRunExecutor(_BlockingGraph())

    await executor.cancel(None)
    await executor.cancel("missing")

