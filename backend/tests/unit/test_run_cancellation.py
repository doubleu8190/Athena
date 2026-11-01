from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from domain.approval import ApprovalDecision
from infrastructure.persistence.postgres.models import (
    AgentPlanModel,
    AgentRunModel,
    ApprovalRecordModel,
)
from infrastructure.persistence.postgres.repositories.approval_repository import (
    PostgresApprovalRepository,
)


class Result:
    def __init__(self):
        self.rowcount = 1


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

    async def get(self, model, _key, **_kwargs):
        row = self.rows.pop(0)
        if model is AgentRunModel and row.status != "stopping":
            row.status = "stopping"
        return row

    async def execute(self, statement):
        self.executed.append(statement)
        return Result()

    async def scalar(self, _statement):
        return datetime.now(timezone.utc)


def factory(session):
    @asynccontextmanager
    async def provide():
        yield session

    return provide


@pytest.mark.asyncio
async def test_approval_resolution_is_rejected_after_run_enters_stopping():
    approval = SimpleNamespace(
        run_id="run-1", plan_id="plan-1", execution_id=None,
        approval_request_id=None, status="pending", decision=None,
    )
    run = SimpleNamespace(status="stopping")
    plan = SimpleNamespace(run_id="run-1", status="stopping")
    session = ApprovalSession([approval, run, plan, approval])
    repository = PostgresApprovalRepository(factory(session))

    accepted = await repository.resolve(
        "approval-1",
        decision=ApprovalDecision.APPROVED,
    )

    assert accepted is False
    assert session.executed == []


@pytest.mark.parametrize(
    ("stored_decision", "requested_decision", "expected"),
    [
        ("approved", ApprovalDecision.APPROVED, True),
        ("approved", ApprovalDecision.DENIED, False),
    ],
)
@pytest.mark.asyncio
async def test_resolved_approval_replay_is_idempotent_only_for_same_decision(
    stored_decision, requested_decision, expected
):
    """响应丢失后重放同一决定成功，相反决定仍然冲突。"""
    approval = SimpleNamespace(
        run_id="run-1",
        plan_id="plan-1",
        execution_id=None,
        approval_request_id=None,
        status="resolved",
        decision=stored_decision,
    )
    run = SimpleNamespace(status="running")
    plan = SimpleNamespace(run_id="run-1", status="running")

    class ReplaySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def begin(self):
            return self

        async def get(self, model, _key, **_kwargs):
            if model is AgentRunModel:
                return run
            if model is AgentPlanModel:
                return plan
            if model is ApprovalRecordModel:
                return approval
            return None

        async def execute(self, _statement):
            raise AssertionError("idempotent replay must not write a second decision")

        async def scalar(self, _statement):
            raise AssertionError("idempotent replay should return before clock lookup")

    repository = PostgresApprovalRepository(factory(ReplaySession()))
    accepted = await repository.resolve(
        "approval-1",
        decision=requested_decision,
    )

    assert accepted is expected
