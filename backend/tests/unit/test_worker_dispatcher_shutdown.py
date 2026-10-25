from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from domain.orchestration import Task, TaskExecution, TaskResult, TaskStatus, WorkerJobLease
from workers.worker_job_dispatcher import WorkerJobDispatcher
from tests.fakes.worker_runtime import (
    AcceptingWorkerApprovalService,
    NoopGraphThreadLock,
)


@pytest.mark.asyncio
async def test_dispatcher_stop_cancels_inflight_graph_without_waiting_for_task_timeout():
    class Jobs:
        def __init__(self):
            self.claimed = False
            self.acknowledged = False

        async def reconcile_cancellations(self):
            return []

        async def claim_next(self, _worker_id, *, lease_seconds):
            if self.claimed:
                return None
            self.claimed = True
            return lease

        async def renew_lease(self, current_lease, *, lease_seconds):
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

        async def pending_interrupt(self, _execution):
            return None

    class EventHandler:
        async def apply_worker_terminal_event(self, _event):
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
        approval_service=AcceptingWorkerApprovalService(),
        thread_lock=NoopGraphThreadLock(),
    )

    await dispatcher.start()
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    await asyncio.wait_for(dispatcher.stop(), timeout=1)

    assert worker.cancelled.is_set()
    assert jobs.acknowledged is False


@pytest.mark.asyncio
async def test_dispatcher_holds_thread_lock_for_graph_invocation():
    class Lock:
        active_thread = None

        @asynccontextmanager
        async def hold(self, thread_id):
            assert self.active_thread is None
            self.active_thread = thread_id
            try:
                yield
            finally:
                self.active_thread = None

    class Worker:
        async def execute(self, _execution):
            assert lock.active_thread == lease.worker_thread_id
            return TaskResult(task_id="task-1", status=TaskStatus.COMPLETED)

        async def resume(self, _execution, _resume_value):
            raise AssertionError("execute job must start the Worker graph")

        async def pending_interrupt(self, _execution):
            return None

    class Jobs:
        pass

    class EventHandler:
        pass

    lease = WorkerJobLease(
        job_id="job-locked",
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
        task=Task("task-1", "locked task", "verify exclusive invocation"),
    )
    lock = Lock()
    dispatcher = WorkerJobDispatcher(
        Jobs(),
        Worker(),
        EventHandler(),
        worker_id="worker-1",
        lease_seconds=30,
        max_concurrency=1,
        approval_service=AcceptingWorkerApprovalService(),
        thread_lock=lock,
    )
    execution = TaskExecution(
        plan_id=lease.plan_id,
        run_id=lease.run_id,
        session_id=lease.session_id,
        task=lease.task,
        execution_generation=lease.attempt,
        execution_id=lease.execution_id,
        worker_thread_id=lease.worker_thread_id,
        fencing_token=lease.fencing_token,
        cancellation_epoch=lease.cancellation_epoch,
    )

    result = await dispatcher._run_worker(lease, execution)

    assert result.status is TaskStatus.COMPLETED
    assert lock.active_thread is None
