from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from domain.orchestration import (
    RunCancellationReceipt,
    Task,
    TaskResult,
    TaskStatus,
    WorkerJobLease,
)
from infrastructure.persistence.postgres.repositories.worker_job_repository import (
    PostgresWorkerJobRepository,
)
from infrastructure.persistence.postgres.models import OrchestrationOutboxModel
from workers.worker_job_dispatcher import WorkerJobDispatcher
from tests.fakes.worker_runtime import (
    AcceptingWorkerApprovalService,
    NoopGraphThreadLock,
)

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


class Result:
    def __init__(self, *, scalar=None, rows=()):
        self._scalar = scalar
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows


class ChainSession:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return self

    async def execute(self, statement):
        self.executed.append(statement)
        return Result(scalar=self.rows.pop(0))

    def add(self, value):
        self.added.append(value)


class ReconcileSession:
    def __init__(self, run, plan, expired_rows=()):
        self.run = run
        self.plan = plan
        self.expired_rows = list(expired_rows)
        self.execute_count = 0
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return self

    async def get(self, model, _key, **_kwargs):
        return self.run if model.__name__ == "AgentRunModel" else None

    async def execute(self, _statement):
        self.execute_count += 1
        if self.execute_count == 1:
            return Result(rows=[self.run.run_id])
        if self.execute_count == 2:
            return Result(scalar=self.plan)
        if self.execute_count == 3:
            return Result(rows=self.expired_rows)
        if self.execute_count == 4 + 4 * len(self.expired_rows):
            return Result(scalar=0)
        return Result()

    def add(self, value):
        self.added.append(value)

    def add_all(self, values):
        self.added.extend(values)


class ClaimSession:
    def __init__(self):
        self.added = []

    async def execute(self, _statement):
        return Result(scalar=0)

    def add(self, value):
        self.added.append(value)


def factory(session):
    @asynccontextmanager
    async def provide():
        yield session

    return provide


def cancellation_lease() -> WorkerJobLease:
    return WorkerJobLease(
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
        fencing_token=7,
        cancellation_epoch=3,
        lease_until=NOW + timedelta(seconds=10),
        task=Task("task-1", "Task", "Do task"),
    )


@pytest.mark.asyncio
async def test_worker_claim_commits_task_started_outbox_with_execution_lease(
    monkeypatch,
):
    session = ClaimSession()
    run = SimpleNamespace(
        run_id="run-1", session_id="session-1", status="running", cancellation_epoch=0
    )
    plan = SimpleNamespace(
        plan_id="plan-1",
        run_id="run-1",
        status="running",
        active_round_id="round-1",
        current_version=1,
        cancellation_epoch=0,
        max_parallelism=2,
    )
    round_row = SimpleNamespace(
        round_id="round-1", plan_version=1, status="running"
    )
    task_row = SimpleNamespace(
        task_id="task-1",
        plan_id="plan-1",
        run_id="run-1",
        status="pending",
        active_execution_id="execution-1",
        execution_generation=1,
        worker_thread_id="task:task-1:exec:1",
        title="Write report",
        objective="Write the requested report",
        input_json="{}",
        expected_output_json="{}",
        allowed_tools_json="[]",
        worker_type="general",
        updated_at=None,
    )
    round_task = SimpleNamespace(
        status="pending", active_execution_id="execution-1", terminal_at=None
    )
    execution = SimpleNamespace(
        execution_id="execution-1",
        worker_thread_id="task:task-1:exec:1",
        status="queued",
        cancellation_epoch=0,
        fencing_token=1,
        lease_owner=None,
        lease_until=None,
        attempt=1,
        task_id="task-1",
        round_id="round-1",
        updated_at=None,
    )
    job = SimpleNamespace(
        job_type="execute",
        status="pending",
        lease_owner=None,
        lease_until=None,
        attempt=0,
        available_at=NOW - timedelta(seconds=1),
        updated_at=None,
        job_id="job-1",
        execution_id="execution-1",
    )
    repository = PostgresWorkerJobRepository(
        factory(session), clock=lambda: NOW
    )

    async def lock_chain(*_args, **_kwargs):
        return run, plan, round_row, task_row, round_task, execution, job

    async def upstream(*_args, **_kwargs):
        return ()

    monkeypatch.setattr(repository, "_lock_execution_chain", lock_chain)
    monkeypatch.setattr(repository, "_load_upstream_results", upstream)
    lease = await repository._claim_candidate(
        session,
        SimpleNamespace(
            job_id="job-1",
            run_id="run-1",
            plan_id="plan-1",
            round_id="round-1",
            task_id="task-1",
            execution_id="execution-1",
            job_type="execute",
            approval_id=None,
            resume_value={},
        ),
        worker_id="worker-1",
        now=NOW,
        lease_seconds=30,
    )

    assert lease is not None
    event = next(
        item for item in session.added if isinstance(item, OrchestrationOutboxModel)
    )
    assert event.event_type == "TaskStarted"
    assert event.dedupe_key == "execution:execution-1:started"
    assert event.payload_json["attempt"] == 1
    assert event.payload_json["parent_activity_id"] == "round:round-1"


@pytest.mark.asyncio
async def test_worker_acknowledges_only_its_fenced_cancellation():
    run = SimpleNamespace(
        run_id="run-1", status="cancelling", cancellation_epoch=4
    )
    plan = SimpleNamespace(
        plan_id="plan-1",
        status="cancelling",
        cancellation_epoch=4,
        current_version=1,
    )
    round_row = SimpleNamespace(
        round_id="round-1",
        plan_version=1,
        terminal_task_count=0,
        expected_task_count=1,
    )
    task = SimpleNamespace(
        task_id="task-1",
        title="Task",
        objective="Do task",
        status="cancelling",
        updated_at=None,
    )
    round_task = SimpleNamespace(status="cancelling", terminal_at=None)
    execution = SimpleNamespace(
        status="cancel_requested",
        execution_id="execution-1",
        cancellation_epoch=4,
        lease_owner="worker-1",
        lease_until=NOW,
        updated_at=NOW,
        fencing_token=7,
        attempt=1,
    )
    job = SimpleNamespace(
        status="cancel_requested",
        lease_owner="worker-1",
        lease_until=NOW,
        updated_at=NOW,
    )
    session = ChainSession([run, plan, round_row, task, round_task, execution, job])
    repository = PostgresWorkerJobRepository(
        factory(session), clock=lambda: NOW
    )

    accepted = await repository.acknowledge_cancelled(cancellation_lease())

    assert accepted is True
    assert job.status == execution.status == task.status == round_task.status == "cancelled"
    assert job.lease_owner is execution.lease_owner is None
    assert round_row.terminal_task_count == 1
    event = next(
        item for item in session.added if isinstance(item, OrchestrationOutboxModel)
    )
    assert event.event_type == "TaskCancelled"
    assert event.dedupe_key == "task:task-1:cancelled:4"


@pytest.mark.asyncio
async def test_reconciler_finishes_cancel_when_no_workers_remain():
    run = SimpleNamespace(
        run_id="run-1",
        session_id="session-1",
        status="cancelling",
        cancellation_epoch=5,
        cancellation_external_effect_status="none",
        updated_at=None,
    )
    plan = SimpleNamespace(
        plan_id="plan-1",
        status="cancelling",
        cancellation_epoch=5,
        current_version=2,
        active_round_id="round-2",
        updated_at=None,
    )
    session = ReconcileSession(run, plan)
    repository = PostgresWorkerJobRepository(factory(session), clock=lambda: NOW)

    receipts = await repository.reconcile_cancellations()

    assert receipts == [
        RunCancellationReceipt("run-1", "session-1", "plan-1", 5, "none")
    ]
    assert run.status == plan.status == "cancelled"
    assert [row.event_type for row in session.added] == [
        "PlanCancelled",
        "RunCancelled",
    ]


@pytest.mark.asyncio
async def test_reconciler_persists_task_cancelled_when_worker_lease_expires():
    run = SimpleNamespace(
        run_id="run-1",
        session_id="session-1",
        status="cancelling",
        cancellation_epoch=5,
        cancellation_external_effect_status="unknown",
        updated_at=None,
    )
    plan = SimpleNamespace(
        plan_id="plan-1",
        status="cancelling",
        cancellation_epoch=5,
        current_version=2,
        active_round_id="round-2",
        updated_at=None,
    )
    session = ReconcileSession(
        run,
        plan,
        expired_rows=[
            (
                "job-1",
                "execution-1",
                "task-1",
                "round-2",
                "Task",
                "Do task",
                1,
            )
        ],
    )
    repository = PostgresWorkerJobRepository(factory(session), clock=lambda: NOW)

    receipts = await repository.reconcile_cancellations()

    task_event = next(
        item for item in session.added if isinstance(item, OrchestrationOutboxModel)
        and item.event_type == "TaskCancelled"
    )
    assert receipts[0].external_effect_status == "unknown"
    assert task_event.task_id == "task-1"
    assert task_event.execution_id == "execution-1"
    assert task_event.dedupe_key == "task:task-1:cancelled:5"


@pytest.mark.asyncio
async def test_dispatcher_acknowledges_cancellation_after_lease_fence_fails():
    class Jobs:
        def __init__(self):
            self.acknowledged = []

        async def renew_lease(self, _lease):
            return None

        async def acknowledge_cancelled(self, lease):
            self.acknowledged.append(lease.execution_id)
            return True

    class Worker:
        async def execute(self, execution):
            await asyncio.Event().wait()
            return TaskResult(execution.task.task_id, TaskStatus.COMPLETED)

        async def resume(self, _execution, _resume_value):
            raise AssertionError("execute job must not resume the Worker")

        async def pending_interrupt(self, _execution):
            return None

    class EventHandler:
        pass

    jobs = Jobs()
    dispatcher = WorkerJobDispatcher(
        jobs,
        Worker(),
        EventHandler(),
        worker_id="dispatcher-1",
        lease_seconds=0.1,
        max_concurrency=1,
        approval_service=AcceptingWorkerApprovalService(),
        thread_lock=NoopGraphThreadLock(),
    )

    await asyncio.wait_for(dispatcher._execute(cancellation_lease()), timeout=1)

    assert jobs.acknowledged == ["execution-1"]
