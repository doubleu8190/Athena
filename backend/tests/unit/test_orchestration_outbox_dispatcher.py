"""编排 Outbox 投递与失败重试测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from domain.orchestration import OrchestrationOutboxLease
from workers.orchestration_outbox_dispatcher import OrchestrationOutboxDispatcher


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def lease(
    *,
    attempt: int = 1,
    event_type: str = "RunCancelled",
    payload: dict[str, Any] | None = None,
):
    return OrchestrationOutboxLease(
        message_id="message-1",
        worker_id="dispatcher-1",
        event_type=event_type,
        run_id="run-1",
        plan_id="plan-1",
        session_id="session-1",
        plan_version=2,
        round_id="round-2",
        occurred_at=NOW,
        payload=payload
        or {"cancellation_epoch": 4, "external_effect_status": "unknown"},
        attempt=attempt,
        lease_until=NOW,
    )


class Outbox:
    def __init__(self, value):
        self.value = value
        self.published = []
        self.retries = []

    async def claim_next_event(self, _worker_id, *, lease_seconds):
        value, self.value = self.value, None
        return value

    async def mark_event_published(self, value):
        self.published.append(value.message_id)
        return True

    async def retry_event(self, value, *, delay_seconds):
        self.retries.append((value.message_id, delay_seconds))
        return True


class Events:
    def __init__(self, *, error=None):
        self.values = []
        self.error = error

    async def publish(self, event):
        if self.error is not None:
            raise self.error
        self.values.append(event)
        return event


@pytest.mark.asyncio
async def test_dispatcher_persists_stable_run_cancelled_event_before_ack():
    outbox = Outbox(lease())
    events = Events()
    dispatcher = OrchestrationOutboxDispatcher(
        outbox, events, worker_id="dispatcher-1"
    )

    assert await dispatcher.dispatch_once() is True
    event = events.values[0]
    assert event.event_type == "run.cancelled"
    assert event.run_id == "run-1"
    assert event.session_id == "session-1"
    assert event.transition_id == "orchestration-outbox:message-1"
    assert event.payload["cancellation_epoch"] == 4
    assert outbox.published == ["message-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "expected_type", "activity_id", "status", "payload"),
    [
        (
            "PlanAccepted",
            "plan.accepted",
            "plan:plan-1",
            "running",
            {"task_ids": ["task-1"]},
        ),
        (
            "PlanCompleted",
            "plan.completed",
            "plan:plan-1",
            "completed",
            {"goal": "goal"},
        ),
        (
            "PlanFailed",
            "plan.failed",
            "plan:plan-1",
            "failed",
            {"goal": "goal"},
        ),
        (
            "PlanCancelled",
            "plan.cancelled",
            "plan:plan-1",
            "cancelled",
            {"cancellation_epoch": 4},
        ),
        (
            "RoundStarted",
            "round.started",
            "round:round-2",
            "running",
            {"task_ids": ["task-1", "task-2"]},
        ),
        (
            "RoundCompleted",
            "round.completed",
            "round:round-2",
            "completed",
            {"terminal_task_count": 2, "expected_task_count": 2},
        ),
    ],
)
async def test_dispatcher_maps_durable_plan_and_round_events(
    event_type, expected_type, activity_id, status, payload
):
    outbox = Outbox(lease(event_type=event_type, payload=payload))
    events = Events()
    dispatcher = OrchestrationOutboxDispatcher(
        outbox, events, worker_id="dispatcher-1"
    )

    assert await dispatcher.dispatch_once() is True
    event = events.values[0]
    assert event.event_type == expected_type
    assert event.durability == "durable"
    assert event.transition_id == "orchestration-outbox:message-1"
    assert event.payload["activity_id"] == activity_id
    assert event.payload["status"] == status
    for key, value in payload.items():
        assert event.payload[key] == value
    assert outbox.published == ["message-1"]


@pytest.mark.asyncio
async def test_dispatcher_retries_failed_event_with_bounded_backoff():
    outbox = Outbox(lease(attempt=3, event_type="PlanCancelling"))
    events = Events(error=RuntimeError("event store unavailable"))
    dispatcher = OrchestrationOutboxDispatcher(
        outbox, events, worker_id="dispatcher-1"
    )

    assert await dispatcher.dispatch_once() is True
    assert outbox.published == []
    assert outbox.retries == [("message-1", 2.0)]


@pytest.mark.asyncio
async def test_dispatcher_returns_false_when_outbox_is_empty():
    dispatcher = OrchestrationOutboxDispatcher(
        Outbox(None), Events(), worker_id="dispatcher-1"
    )

    assert await dispatcher.dispatch_once() is False
