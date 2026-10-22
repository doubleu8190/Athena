from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from domain.orchestration import (
    Task,
    TaskResult,
    TaskStatus,
    WorkerJobLease,
)
from workers.worker_job_dispatcher import WorkerJobDispatcher


@pytest.mark.asyncio
async def test_worker_timeout_cancels_graph_and_reports_retryable_timeout():
    class Jobs:
        async def renew_lease(self, lease):
            return lease

    class Worker:
        def __init__(self):
            self.cancelled = asyncio.Event()

        async def execute(self, _execution):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

        async def resume(self, _execution, _resume_value):
            raise AssertionError("execute job must start the Worker graph")

        async def pending_interrupt(self, _execution):
            return None

    class EventHandler:
        def __init__(self):
            self.events = []

        async def apply_terminal_event(self, event):
            self.events.append(event)
            return SimpleNamespace(accepted=True, reason=None)

    worker = Worker()
    event_handler = EventHandler()
    dispatcher = WorkerJobDispatcher(
        Jobs(),
        worker,
        event_handler,
        worker_id="worker-1",
        lease_seconds=30,
        max_concurrency=1,
    )
    lease = WorkerJobLease(
        job_id="job-1",
        worker_id="worker-1",
        run_id="run-1",
        plan_id="plan-1",
        round_id="round-1",
        plan_version=1,
        task_id="task-1",
        execution_id="execution-1",
        session_id="session-1",
        attempt=1,
        worker_thread_id="task:task-1:exec:1",
        fencing_token=1,
        cancellation_epoch=0,
        lease_until=datetime.now(timezone.utc),
        task=Task("task-1", "bounded", "finish promptly", timeout_seconds=0.01),
    )

    await dispatcher._execute(lease)

    assert worker.cancelled.is_set()
    assert len(event_handler.events) == 1
    event = event_handler.events[0]
    assert event.status is TaskStatus.FAILED
    assert event.retryable is True
    assert event.error == {"code": "task_timeout", "timeout_seconds": 0.01}
