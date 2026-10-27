from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from langgraph.types import Interrupt

from domain.runs import (
    CommandType,
    RoundCompletedResumeValue,
    RunCommand,
    RunResumePayload,
    RootWaitCheckpoint,
    RunStartPayload,
)
from application.orchestration.langgraph import LangGraphRunExecutor
from tests.fakes.leases import lease_grant


def _command(run_id: str = "run-1") -> RunCommand:
    return RunCommand(
        command_id=f"command-{run_id}",
        command_type=CommandType.RUN_START,
        session_id="session-1",
        run_id=run_id,
        payload=RunStartPayload(message="hello", message_id="message-1"),
        issued_at=datetime.now(timezone.utc),
        lease=lease_grant("root_execution", run_id),
    )


class _BlockingGraph:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def invoke(self, **kwargs):
        self.started.set()
        await asyncio.Event().wait()


class _InterruptedGraph:
    async def invoke(self, **kwargs):
        return {
            "__interrupt__": [
                Interrupt(
                    id="interrupt-1",
                    value={
                        "type": "round_wait",
                        "run_id": "run-1",
                        "plan_id": "plan-1",
                        "round_id": "round-1",
                        "plan_version": 2,
                    },
                )
            ],
            "__root_checkpoint_id__": "checkpoint-1",
        }


class _CapturingGraph:
    def __init__(self) -> None:
        self.arguments: dict[str, object] = {}

    async def invoke(self, **kwargs):
        self.arguments = kwargs
        return {"result": {"content": "resumed answer"}}


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


@pytest.mark.asyncio
async def test_langgraph_interrupt_is_returned_as_waiting_execution() -> None:
    executor = LangGraphRunExecutor(_InterruptedGraph())

    outcome = await executor.execute(_command())

    assert outcome.waiting is True
    assert outcome.root_wait == RootWaitCheckpoint(
        run_id="run-1",
        plan_id="plan-1",
        round_id="round-1",
        plan_version=2,
        interrupt_id="interrupt-1",
        checkpoint_id="checkpoint-1",
    )
    assert outcome.result["__root_checkpoint_id__"] == "checkpoint-1"
    assert outcome.result["__interrupt__"][0].id == "interrupt-1"


@pytest.mark.asyncio
async def test_resume_uses_only_checkpoint_resume_identity() -> None:
    graph = _CapturingGraph()
    executor = LangGraphRunExecutor(graph)
    command = RunCommand(
        command_id="resume-command-1",
        command_type=CommandType.RUN_RESUME,
        session_id="session-1",
        run_id="run-1",
        payload=RunResumePayload(
            resume_value=RoundCompletedResumeValue(
                type="round_completed",
                run_id="run-1",
                plan_id="plan-1",
                round_id="round-1",
                plan_version=1,
                root_wait_generation=1,
                interrupt_id="interrupt-1",
            )
        ),
        issued_at=datetime.now(timezone.utc),
            lease=lease_grant("root_execution", "run-1"),
    )

    await executor.execute(command)

    assert graph.arguments["user_message"] == ""
    assert graph.arguments["message_id"] == ""
    assert graph.arguments["attachment_ids"] == []
    assert graph.arguments["resume_value"] == command.payload.resume_value

