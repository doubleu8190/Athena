"""Root invocation outcomes update the single Run lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from application.runs import RunExecutionService
from domain.events import ApplicationEvent, EventType
from domain.runs import (
    CommandExecutionResult,
    CommandType,
    RootWaitCheckpoint,
    RunCommand,
    RunStartPayload,
    RunStatus,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Lifecycle:
    writes_cancellation_events_to_outbox = False
    writes_terminal_artifacts_to_outbox = False

    def __init__(self) -> None:
        self.status_updates = []
        self.completed = []
        self.failed = []
        self.waits = []
        self.complete_result = True

    async def update_status(self, run_id, status):
        self.status_updates.append((run_id, status))
        return True

    async def complete(self, run_id, result):
        self.completed.append((run_id, result))
        return self.complete_result

    async def fail(self, run_id, error):
        self.failed.append((run_id, error))
        return True

    async def arm_root_wait(self, run_id, **values):
        self.waits.append((run_id, values))
        return True


class Executor:
    def __init__(self, outcome):
        self.outcome = outcome

    async def execute(self, invocation):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def cancel(self, run_id):
        return None


class Events:
    def __init__(self) -> None:
        self.items: list[ApplicationEvent] = []

    async def publish(self, event):
        self.items.append(event)
        return event


def invocation() -> RunCommand:
    return RunCommand(
        "run:run-1:generation:1",
        CommandType.RUN_START,
        "session-1",
        "run-1",
        RunStartPayload(message="hello", message_id="message-1"),
        NOW,
    )


def answer_result() -> dict[str, object]:
    return {
        "content": "hello back",
        "answer_stream": {
            "stream_id": "stream-1",
            "last_chunk_id": 2,
            "started_at": NOW.isoformat(),
        },
    }


@pytest.mark.asyncio
async def test_success_completes_run_and_publishes_the_reply():
    lifecycle, events = Lifecycle(), Events()
    service = RunExecutionService(
        lifecycle, Executor(CommandExecutionResult(result=answer_result())), events
    )

    result = await service.execute(invocation())

    assert result == answer_result()
    assert lifecycle.status_updates == [("run-1", RunStatus.RUNNING)]
    assert lifecycle.completed == [("run-1", answer_result())]
    assert [event.event_type for event in events.items] == [
        EventType.RUN_STARTED,
        EventType.MESSAGE_COMPLETED,
        EventType.RUN_COMPLETED,
    ]
    assert events.items[1].payload["reply_to_message_id"] == "message-1"


@pytest.mark.asyncio
async def test_root_wait_keeps_run_open_and_arms_checkpoint():
    lifecycle, events = Lifecycle(), Events()
    checkpoint = RootWaitCheckpoint(
        "run-1", "plan-1", "round-1", 1, "interrupt-1", "checkpoint-1"
    )
    service = RunExecutionService(
        lifecycle, Executor(CommandExecutionResult(root_wait=checkpoint)), events
    )

    result = await service.execute(invocation())

    assert result is None
    assert lifecycle.completed == []
    assert lifecycle.failed == []
    assert lifecycle.waits[0][0] == "run-1"
    assert events.items[-1].event_type == EventType.RUN_ROOT_SUSPENDED


@pytest.mark.asyncio
async def test_executor_error_marks_the_run_failed():
    lifecycle, events = Lifecycle(), Events()
    service = RunExecutionService(
        lifecycle, Executor(CommandExecutionResult(error={"code": "bad_input"})), events
    )

    await service.execute(invocation())

    assert lifecycle.failed == [("run-1", {"code": "bad_input"})]
    assert events.items[-1].event_type == EventType.RUN_FAILED


@pytest.mark.asyncio
async def test_executor_exception_is_converted_to_a_run_failure():
    lifecycle, events = Lifecycle(), Events()
    service = RunExecutionService(lifecycle, Executor(RuntimeError("boom")), events)

    await service.execute(invocation())

    assert lifecycle.failed == [
        ("run-1", {"code": "run_failed", "message": "boom"})
    ]
    assert events.items[-1].event_type == EventType.RUN_FAILED
