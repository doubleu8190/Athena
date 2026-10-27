from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from domain.approval import ApprovalDecision
from infrastructure.persistence.postgres.models import (
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


def factory(session):
    @asynccontextmanager
    async def provide():
        yield session

    return provide


@pytest.mark.asyncio
async def test_approval_resolution_is_rejected_after_run_enters_stopping():
    approval = SimpleNamespace(run_id="run-1", plan_id="plan-1")
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
