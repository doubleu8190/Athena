from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from domain.orchestration import TaskStatus, WorkerTaskTerminalEvent
from infrastructure.persistence.postgres.models import OrchestrationOutboxModel
from infrastructure.persistence.postgres.repositories.orchestration_event_repository import (
    PostgresOrchestrationEventRepository,
    _event_document,
    _hash,
)


NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


class Result:
    def __init__(self, value=None):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class Session:
    def __init__(self, rows):
        self.rows = list(rows)
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return self

    async def execute(self, _statement):
        return Result(self.rows.pop(0))

    def add(self, value):
        self.added.append(value)


def factory(session):
    @asynccontextmanager
    async def provide():
        yield session

    return provide


@pytest.mark.parametrize(
    ("status", "retryable", "event_type", "task_status", "terminal_count"),
    [
        (TaskStatus.COMPLETED, False, "TaskCompleted", "completed", 1),
        (TaskStatus.FAILED, False, "TaskFailed", "failed", 1),
        (TaskStatus.FAILED, True, "TaskRetrying", "pending", 0),
    ],
)
@pytest.mark.asyncio
async def test_worker_terminal_transaction_persists_task_event_before_round_barrier(
    status, retryable, event_type, task_status, terminal_count
):
    result = {"summary": "report ready"} if status == TaskStatus.COMPLETED else {}
    error = None if status == TaskStatus.COMPLETED else {"code": "temporary"}
    event = WorkerTaskTerminalEvent(
        message_id="worker-event-1",
        occurred_at=NOW,
        worker_id="worker-1",
        run_id="run-1",
        plan_id="plan-1",
        plan_version=1,
        round_id="round-1",
        task_id="task-1",
        execution_id="execution-1",
        fencing_token=3,
        cancellation_epoch=0,
        status=status,
        result=result,
        error=error,
        retryable=retryable,
    )
    inbox = SimpleNamespace(
        payload_hash=_hash(_event_document(event)),
        status="received",
        receipt_json=None,
        processed_at=None,
    )
    run = SimpleNamespace(
        run_id="run-1",
        session_id="session-1",
        status="running",
        cancellation_epoch=0,
        root_wait_round_id=None,
        root_wait_plan_version=None,
        root_wait_generation=0,
        root_wait_armed_at=None,
        root_wait_interrupt_id=None,
    )
    plan = SimpleNamespace(
        plan_id="plan-1",
        run_id="run-1",
        session_id="session-1",
        status="running",
        cancellation_epoch=0,
        current_version=1,
        active_round_id="round-1",
    )
    round_row = SimpleNamespace(
        plan_id="plan-1",
        round_id="round-1",
        plan_version=1,
        status="running",
        expected_task_count=2,
        terminal_task_count=0,
        completed_at=None,
    )
    task = SimpleNamespace(
        task_id="task-1",
        run_id="run-1",
        plan_id="plan-1",
        status="running",
        active_execution_id="execution-1",
        title="Write report",
        objective="Write the requested report",
        worker_type="general",
        output_json=None,
        error_json=None,
        updated_at=None,
    )
    round_task = SimpleNamespace(
        status="running", active_execution_id="execution-1", terminal_at=None
    )
    execution = SimpleNamespace(
        execution_id="execution-1",
        task_id="task-1",
        round_id="round-1",
        status="running",
        cancellation_epoch=0,
        fencing_token=3,
        lease_owner="worker-1",
        lease_until=NOW + timedelta(seconds=30),
        attempt=1,
        result_ref=None,
        result_json=None,
        error_json=None,
        updated_at=None,
    )
    job = SimpleNamespace(
        status="running",
        lease_owner="worker-1",
        lease_until=NOW + timedelta(seconds=30),
        updated_at=None,
    )
    session = Session([None, inbox, run, plan, round_row, task, round_task, execution, job])
    repository = PostgresOrchestrationEventRepository(
        factory(session), clock=lambda: NOW
    )

    receipt = await repository.apply_worker_terminal_event(event)

    task_event = next(
        item for item in session.added if isinstance(item, OrchestrationOutboxModel)
    )
    assert receipt.accepted is True
    assert receipt.round_completed is False
    assert receipt.terminal_task_count == terminal_count
    assert task.status == round_task.status == task_status
    assert execution.status == status.value
    assert task_event.event_type == event_type
    expected_transition = "retrying" if retryable else "terminal"
    assert task_event.dedupe_key == f"execution:execution-1:{expected_transition}"
    if status == TaskStatus.COMPLETED:
        assert task_event.payload_json["result"] == result
    if retryable:
        assert receipt.retry_scheduled is True
        assert task_event.payload_json["next_execution_id"] == receipt.next_execution_id
    assert inbox.status == "processed"
