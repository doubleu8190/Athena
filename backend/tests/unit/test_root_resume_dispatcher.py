from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from domain.runs import RootPlanCompletedResumeValue
from tests.fakes.leases import LeaseCenter, lease_grant
from workers.root_resume_dispatcher import RootResumeDispatcher


def _lease(*, interrupt_id: str | None = "interrupt-1", checkpoint_id: str | None = "checkpoint-1"):
    return SimpleNamespace(
        job_id="job-1", run_id="run-1", session_id="session-1", plan_id="plan-1",
        sequence=1, root_wait_generation=2, interrupt_id=interrupt_id,
        checkpoint_id=checkpoint_id, lease=lease_grant("root_resume", "job-1"),
    )


class _Jobs:
    def __init__(self, *, current: bool = True) -> None:
        self.current = current
        self.completed: list[object] = []
        self.retried: list[object] = []

    async def is_wait_current(self, _lease):
        return self.current

    async def complete(self, lease):
        self.completed.append(lease)
        return True

    async def retry(self, lease, *, delay_seconds):
        self.retried.append((lease, delay_seconds))
        return True


class _Lifecycle:
    async def acquire_execution_lease(self, run_id, *, owner, lease_seconds):
        return lease_grant("root_execution", run_id, owner_id=owner)

    async def arm_root_wait(self, *_args, **_kwargs):
        return True


class _Runs:
    status = "running"

    async def get(self, _run_id):
        return SimpleNamespace(status=self.status)


@pytest.mark.asyncio
async def test_dispatcher_limits_inflight_claims_to_eight():
    class Jobs:
        def __init__(self):
            self.claims = 0

        async def claim_next(self, *_args, **_kwargs):
            self.claims += 1
            return SimpleNamespace(job_id=str(self.claims))

    jobs = Jobs()
    dispatcher = RootResumeDispatcher(
        jobs, None, None, None, None, worker_id="test", poll_interval=0.01,
        lease_center=LeaseCenter(),
    )
    release = asyncio.Event()

    async def hold(_lease):
        await release.wait()

    dispatcher._resume = hold
    await dispatcher.start()
    try:
        await asyncio.sleep(0.08)
        assert jobs.claims == 8
        assert len(dispatcher._inflight) == 8
    finally:
        release.set()
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_dispatcher_resumes_matching_plan_wait_and_converges_job():
    jobs = _Jobs()

    class Graph:
        async def has_pending_plan_wait(self, **_kwargs):
            return True

        async def find_pending_plan_wait(self, **_kwargs):
            return None

        async def is_plan_checkpoint_recoverable(self, **_kwargs):
            return False

    class Execution:
        def __init__(self):
            self.command = None

        async def execute(self, command):
            self.command = command
            jobs.current = False

    execution = Execution()
    dispatcher = RootResumeDispatcher(
        jobs, Graph(), execution, _Lifecycle(), _Runs(), worker_id="root", lease_center=LeaseCenter()
    )
    await dispatcher._resume(_lease())
    assert execution.command is not None
    value = execution.command.payload.resume_value
    assert isinstance(value, RootPlanCompletedResumeValue)
    assert value.plan_sequence == 1
    assert len(jobs.completed) == 1


@pytest.mark.asyncio
async def test_dispatcher_retries_when_checkpoint_is_not_ready():
    jobs = _Jobs()

    class Graph:
        async def has_pending_plan_wait(self, **_kwargs):
            return False

        async def find_pending_plan_wait(self, **_kwargs):
            return None

        async def is_plan_checkpoint_recoverable(self, **_kwargs):
            return False

    class Execution:
        async def execute(self, _command):
            raise AssertionError("Root must not resume without a matching checkpoint")

    dispatcher = RootResumeDispatcher(
        jobs, Graph(), Execution(), _Lifecycle(), _Runs(), worker_id="root", lease_center=LeaseCenter()
    )
    await dispatcher._resume(_lease())
    assert jobs.retried and not jobs.completed


@pytest.mark.asyncio
async def test_dispatcher_retries_resume_exception_for_checkpoint_recovery():
    jobs = _Jobs()

    class Graph:
        async def has_pending_plan_wait(self, **_kwargs):
            return True

        async def find_pending_plan_wait(self, **_kwargs):
            return None

        async def is_plan_checkpoint_recoverable(self, **_kwargs):
            return True

    class Execution:
        async def execute(self, _command):
            raise RuntimeError("transient graph failure")

    dispatcher = RootResumeDispatcher(
        jobs, Graph(), Execution(), _Lifecycle(), _Runs(), worker_id="root", lease_center=LeaseCenter()
    )
    await dispatcher._resume(_lease())
    assert jobs.retried and not jobs.completed


@pytest.mark.asyncio
async def test_dispatcher_completes_job_when_wait_is_already_superseded():
    jobs = _Jobs(current=False)

    class Graph:
        async def has_pending_plan_wait(self, **_kwargs):
            raise AssertionError("stale Root wait must not inspect or resume the graph")

    class Execution:
        async def execute(self, _command):
            raise AssertionError("stale Root wait must not invoke the graph")

    dispatcher = RootResumeDispatcher(
        jobs, Graph(), Execution(), _Lifecycle(), _Runs(), worker_id="root", lease_center=LeaseCenter()
    )
    await dispatcher._resume(_lease())
    assert jobs.completed and not jobs.retried


@pytest.mark.asyncio
async def test_dispatcher_uses_checkpoint_recovery_after_interrupt_consumed():
    jobs = _Jobs()

    class Graph:
        async def has_pending_plan_wait(self, **_kwargs):
            return False

        async def find_pending_plan_wait(self, **_kwargs):
            return None

        async def is_plan_checkpoint_recoverable(self, **_kwargs):
            return True

    class Execution:
        def __init__(self):
            self.command = None

        async def execute(self, command):
            self.command = command
            jobs.current = False

    execution = Execution()
    dispatcher = RootResumeDispatcher(
        jobs, Graph(), execution, _Lifecycle(), _Runs(), worker_id="root", lease_center=LeaseCenter()
    )
    await dispatcher._resume(_lease())
    assert execution.command.payload.resume_value.type == "plan_checkpoint_recovery"
    assert jobs.completed
