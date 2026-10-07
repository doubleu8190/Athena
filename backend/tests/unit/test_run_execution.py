"""阶段 5 执行 Service 和 Consumer 测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from application.runs import RunExecutionService
from workers import CommandConsumer
from domain.events import ApplicationEvent
from domain.runs import (
    CommandExecutionResult,
    CommandStatus,
    CommandType,
    RunCommand,
    RunStatus,
)


def command(command_type=CommandType.RUN_START, run_id="run-1"):
    return RunCommand(
        command_id="command-1",
        command_type=command_type,
        session_id="session-1",
        run_id=run_id,
        payload={"message": "hello"},
        issued_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class Lifecycle:
    def __init__(self):
        self.calls = []

    async def update_status(self, run_id, status):
        self.calls.append(("update", run_id, str(status)))
        return True

    async def complete(self, run_id, result):
        self.calls.append(("complete", run_id, result))
        return True

    async def fail(self, run_id, error):
        self.calls.append(("fail", run_id, error))
        return True

    async def cancel(self, run_id):
        self.calls.append(("cancel", run_id))
        return True


class Queue:
    def __init__(self):
        self.completed = []
        self.items = []

    async def claim_next_command(self):
        return self.items.pop(0) if self.items else None

    async def complete_command(self, command_id, *, status, result=None, error=None):
        self.completed.append((command_id, status, result, error))


class Executor:
    def __init__(self, outcome=None, error=None):
        self.outcome = outcome or CommandExecutionResult(result={"answer": "ok"})
        self.error = error
        self.cancelled = []

    async def execute(self, value):
        if self.error:
            raise self.error
        return self.outcome

    async def cancel(self, run_id):
        self.cancelled.append(run_id)


class Events:
    def __init__(self):
        self.values: list[ApplicationEvent] = []

    async def publish(self, event):
        self.values.append(event)
        return event

    async def publish_realtime(self, event):
        self.values.append(event)
        return event


@pytest.mark.asyncio
async def test_execution_service_completes_run_and_command() -> None:
    lifecycle, queue, executor, events = Lifecycle(), Queue(), Executor(), Events()
    service = RunExecutionService(lifecycle, queue, executor, events)

    await service.execute(command())

    assert lifecycle.calls[0] == ("update", "run-1", "running")
    assert lifecycle.calls[1][0] == "complete"
    assert queue.completed[0][1] == CommandStatus.COMPLETED
    assert [event.event_type for event in events.values] == [
        "run.started",
        "run.completed",
    ]


@pytest.mark.asyncio
async def test_execution_service_converges_executor_failure() -> None:
    lifecycle, queue, executor, events = Lifecycle(), Queue(), Executor(error=RuntimeError("boom")), Events()
    service = RunExecutionService(lifecycle, queue, executor, events)

    await service.execute(command())

    assert lifecycle.calls[-1][0] == "fail"
    assert queue.completed[-1][1] == CommandStatus.FAILED
    assert events.values[-1].event_type == "run.failed"


@pytest.mark.asyncio
async def test_execution_service_preserves_resume_and_waiting_events() -> None:
    lifecycle, queue, executor, events = Lifecycle(), Queue(), Executor(
        outcome=CommandExecutionResult(waiting=True)
    ), Events()
    service = RunExecutionService(lifecycle, queue, executor, events)

    await service.execute(command(CommandType.RUN_RESUME))

    assert [event.event_type for event in events.values] == [
        "run.resumed",
        "run.root_suspended",
    ]
    assert queue.completed == []


@pytest.mark.asyncio
async def test_consumer_claims_and_stops_without_runtime_dependency() -> None:
    lifecycle, queue, executor, events = Lifecycle(), Queue(), Executor(), Events()
    queue.items.append(command())
    execution = RunExecutionService(lifecycle, queue, executor, events)
    consumer = CommandConsumer(queue, execution, poll_interval=0.01)

    await consumer.start()
    for _ in range(20):
        if queue.completed:
            break
        await __import__("asyncio").sleep(0.01)
    await consumer.stop()

    assert queue.completed[0][0] == "command-1"
