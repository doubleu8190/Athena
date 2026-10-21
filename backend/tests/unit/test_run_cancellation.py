from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from domain.approval import ApprovalDecision
from domain.runs import RunCancellationResult, RunStatus
from infrastructure.persistence.postgres.repositories.approval_repository import (
    PostgresApprovalRepository,
)
from infrastructure.persistence.postgres.repositories.run_repository import (
    PostgresRunRepository,
)


class Result:
    def __init__(self, *, scalar=None, rows=(), rowcount=1):
        self._scalar = scalar
        self._rows = list(rows)
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows


class CancellationSession:
    def __init__(self, run, plan=None):
        self.run = run
        self.plan = plan
        self.executed = []
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return self

    async def get(self, model, _key, **_kwargs):
        return self.run if model.__name__ == "AgentRunModel" else None

    async def execute(self, statement):
        self.executed.append(statement)
        if len(self.executed) == 1:
            return Result(scalar=self.plan)
        if len(self.executed) == 2 and self.plan is not None:
            return Result(scalar=1)
        if len(self.executed) == 3 and self.plan is not None:
            return Result(rows=[])
        return Result()

    def add(self, value):
        self.added.append(value)


def factory(session):
    @asynccontextmanager
    async def provide():
        yield session

    return provide


@pytest.mark.asyncio
async def test_cancel_plan_advances_epoch_and_persists_cancelling_outbox():
    run = SimpleNamespace(status="running", cancellation_epoch=4, updated_at=None)
    plan = SimpleNamespace(
        plan_id="plan-1",
        run_id="run-1",
        status="running",
        cancellation_epoch=4,
        current_version=2,
        active_round_id="round-2",
        updated_at=None,
    )
    session = CancellationSession(run, plan)
    repository = PostgresRunRepository(factory(session))

    status = await repository.cancel("run-1")

    assert status == RunCancellationResult(RunStatus.CANCELLING, True)
    assert run.status == plan.status == "cancelling"
    assert run.cancellation_epoch == plan.cancellation_epoch == 5
    assert any(
        row.event_type == "PlanCancelling" and row.round_id == "round-2"
        for row in session.added
    )
    assert len(session.executed) >= 10


@pytest.mark.asyncio
async def test_cancel_without_a_plan_finishes_run_and_repeat_cancel_is_noop():
    run = SimpleNamespace(status="created", cancellation_epoch=0, updated_at=None)
    session = CancellationSession(run)
    repository = PostgresRunRepository(factory(session))

    status = await repository.cancel("run-1")
    assert status == RunCancellationResult(RunStatus.CANCELLED, True, "none")
    assert run.status == "cancelled"
    assert run.cancellation_epoch == 1

    already_cancelling = SimpleNamespace(
        status="cancelling",
        cancellation_epoch=1,
        cancellation_external_effect_status="unknown",
        updated_at=None,
    )
    repeated = CancellationSession(already_cancelling)
    assert await PostgresRunRepository(factory(repeated)).cancel("run-1") == (
        RunCancellationResult(RunStatus.CANCELLING, False)
    )
    assert repeated.executed == []


@pytest.mark.asyncio
async def test_cancel_during_root_work_before_plan_marks_effects_unknown():
    run = SimpleNamespace(status="running", cancellation_epoch=2, updated_at=None)
    session = CancellationSession(run)
    repository = PostgresRunRepository(factory(session))

    status = await repository.cancel("run-1")

    assert status == RunCancellationResult(RunStatus.CANCELLED, True, "unknown")
    assert run.cancellation_external_effect_status == "unknown"


class ApprovalSession:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return self

    async def get(self, _model, _key, **_kwargs):
        return self.rows.pop(0)

    async def execute(self, statement):
        self.executed.append(statement)
        return Result()


@pytest.mark.asyncio
async def test_approval_resolution_is_rejected_after_run_enters_cancelling():
    approval = SimpleNamespace(run_id="run-1", plan_id="plan-1")
    run = SimpleNamespace(status="cancelling")
    plan = SimpleNamespace(run_id="run-1", status="cancelling")
    session = ApprovalSession([approval, run, plan, approval])
    repository = PostgresApprovalRepository(factory(session))

    accepted = await repository.resolve(
        "approval-1",
        run_id="run-1",
        task_id="task-1",
        decision=ApprovalDecision.APPROVED,
    )

    assert accepted is False
    assert session.executed == []
