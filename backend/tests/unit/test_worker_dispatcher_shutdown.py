from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from domain.orchestration import Task, WorkerJobLease
from workers.worker_job_dispatcher import WorkerJobDispatcher


@pytest.mark.asyncio
async def test_dispatcher_stop_cancels_inflight_graph_without_waiting_for_task_timeout():
    class Jobs:
        def __init__(self):
            self.claimed = False
            self.acknowledged = False

        async def reconcile_cancellations(self):
            return []

        async def claim_next(self, _worker_id):
            if self.claimed:
                return None
            self.claimed = True
            return lease

        async def renew_lease(self, current_lease):
            return current_lease

        async def acknowledge_cancelled(self, _lease):
            self.acknowledged = True
            return True

    class Worker:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def execute(self, _execution):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

        async def resume(self, _execution, _resume_value):
            raise AssertionError("execute job must start the Worker graph")

    class EventHandler:
        async def apply_terminal_event(self, _event):
            raise AssertionError("shutdown cancellation must not report a Task result")

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
        task=Task("task-1", "long task", "keep working", timeout_seconds=3600),
    )
    jobs = Jobs()
    worker = Worker()
    dispatcher = WorkerJobDispatcher(
        jobs,
        worker,
        EventHandler(),
        worker_id="worker-1",
        lease_seconds=30,
        max_concurrency=1,
        poll_interval=0.01,
    )

    await dispatcher.start()
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    await asyncio.wait_for(dispatcher.stop(), timeout=1)

    assert worker.cancelled.is_set()
    assert jobs.acknowledged is False
