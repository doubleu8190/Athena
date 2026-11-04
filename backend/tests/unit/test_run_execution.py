"""Root invocation outcomes update the single Run lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from application.events import AssistantStreamHub
from application.runs import RunExecutionService
from domain.events import ApplicationEvent, EventDurability, EventType
from domain.runs import (
    CommandExecutionResult,
    CommandType,
    RootWaitCheckpoint,
    RunCommand,
    RunStartPayload,
    RunStatus,
)
from tests.fakes.leases import lease_grant


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Lifecycle:
    writes_cancellation_events_to_outbox = False
    writes_terminal_artifacts_to_outbox = False
    writes_root_wait_event = False

    def __init__(self) -> None:
        self.status_updates = []
        self.completed = []
        self.failed = []
        self.waits = []
        self.complete_result = True

    async def update_status(self, run_id, status, *, lease):
        self.status_updates.append((run_id, status))
        return True

    async def complete(self, run_id, result, *, lease):
        self.completed.append((run_id, result))
        return self.complete_result

    async def fail(self, run_id, error, *, lease):
        self.failed.append((run_id, error))
        return True

    async def arm_root_wait(self, run_id, *, lease, **values):
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
        self.calls: list[tuple[str, ApplicationEvent]] = []

    async def publish(self, event):
        self.items.append(event)
        self.calls.append(("publish", event))
        return event

    async def publish_realtime(self, event):
        self.items.append(event)
        self.calls.append(("publish_realtime", event))
        return event


class PublishFailingEvents(Events):
    async def publish(self, event):
        if event.event_type == EventType.ASSISTANT_COMPLETED:
            raise RuntimeError("event store unavailable")
        return await super().publish(event)


def invocation() -> RunCommand:
    return RunCommand(
        "run:run-1:generation:1",
        CommandType.RUN_START,
        "session-1",
        "run-1",
        RunStartPayload(message="hello", message_id="message-1"),
        NOW,
        lease=lease_grant("root_execution", "run-1"),
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
    assistant_streams = AssistantStreamHub()
    assistant_streams.start("session-1", "run-1", NOW)
    service = RunExecutionService(
        lifecycle,
        Executor(CommandExecutionResult(result=answer_result())),
        events,
        assistant_streams=assistant_streams,
    )

    result = await service.execute(invocation())

    assert result == answer_result()
    assert lifecycle.status_updates == [("run-1", RunStatus.RUNNING)]
    assert lifecycle.completed == [("run-1", answer_result())]
    assert [event.event_type for event in events.items] == [
        EventType.RUN_STARTED,
        EventType.MESSAGE_COMPLETED,
        EventType.ASSISTANT_COMPLETED,
        EventType.RUN_COMPLETED,
    ]
    assert events.items[1].payload["reply_to_message_id"] == "message-1"
    assert events.items[2].durability == EventDurability.DURABLE
    assert events.calls[2][0] == "publish"
    assert assistant_streams.snapshot("session-1", "run-1") is None


@pytest.mark.asyncio
async def test_assistant_completed_publish_failure_keeps_completed_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """完成通知写入失败只记录告警，不把已完成的 Run 改成失败。"""
    lifecycle, events = Lifecycle(), PublishFailingEvents()
    assistant_streams = AssistantStreamHub()
    assistant_streams.start("session-1", "run-1", NOW)
    service = RunExecutionService(
        lifecycle,
        Executor(CommandExecutionResult(result=answer_result())),
        events,
        assistant_streams=assistant_streams,
    )

    result = await service.execute(invocation())

    assert result == answer_result()
    assert lifecycle.failed == []
    assert events.items[-1].event_type == EventType.RUN_COMPLETED
    assert "assistant_completed_publish_failed" in caplog.text
    assert assistant_streams.snapshot("session-1", "run-1") is None


@pytest.mark.asyncio
async def test_root_wait_keeps_run_open_and_arms_checkpoint():
    lifecycle, events = Lifecycle(), Events()
    checkpoint = RootWaitCheckpoint(
        run_id="run-1",
        plan_id="plan-1",
        plan_sequence=1,
        root_wait_generation=1,
        interrupt_id="interrupt-1",
        checkpoint_id="checkpoint-1",
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
async def test_transactional_root_wait_wakes_subscribers_without_republishing():
    lifecycle, events = Lifecycle(), Events()
    lifecycle.writes_root_wait_event = True
    checkpoint = RootWaitCheckpoint(
        run_id="run-1",
        plan_id="plan-1",
        plan_sequence=1,
        root_wait_generation=1,
        interrupt_id="interrupt-1",
        checkpoint_id="checkpoint-1",
    )
    service = RunExecutionService(
        lifecycle, Executor(CommandExecutionResult(root_wait=checkpoint)), events
    )

    await service.execute(invocation())

    assert lifecycle.waits[0][1]["wait_payload"]["status"] == "waiting"
    assert events.items[-1].event_type == EventType.RUN_ROOT_SUSPENDED
    assert events.items[-1].session_seq == 0


@pytest.mark.asyncio
async def test_executor_error_marks_the_run_failed():
    lifecycle, events = Lifecycle(), Events()
    assistant_streams = AssistantStreamHub()
    assistant_streams.start("session-1", "run-1", NOW)
    service = RunExecutionService(
        lifecycle,
        Executor(CommandExecutionResult(error={"code": "bad_input"})),
        events,
        assistant_streams=assistant_streams,
    )

    await service.execute(invocation())

    assert lifecycle.failed == [("run-1", {"code": "bad_input"})]
    assert events.items[-1].event_type == EventType.RUN_FAILED
    assert assistant_streams.snapshot("session-1", "run-1") is None


@pytest.mark.asyncio
async def test_executor_exception_is_logged_with_run_id_and_converted_to_failure(
    caplog: pytest.LogCaptureFixture,
):
    lifecycle, events = Lifecycle(), Events()
    service = RunExecutionService(lifecycle, Executor(RuntimeError("boom")), events)

    await service.execute(invocation())

    assert "run_execution_failed" in caplog.text
    assert "run-1" in caplog.text
    assert "RuntimeError: boom" in caplog.text

    assert lifecycle.failed == [
        ("run-1", {"code": "run_failed", "message": "boom"})
    ]
    assert events.items[-1].event_type == EventType.RUN_FAILED
