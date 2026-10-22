from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from domain.tools import (
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolOperationClaimStatus,
)
from infrastructure.persistence.postgres.models import WorkerToolOperationModel
from infrastructure.persistence.postgres.repositories.worker_tool_operation_repository import (
    PostgresWorkerToolOperationRepository,
)


class Session:
    def __init__(self, rows):
        self.rows = list(rows)
        self.added = []
        self.executed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return self

    async def execute(self, statement):
        self.executed.append(statement)
        return SimpleNamespace()

    async def get(self, *_args, **_kwargs):
        return self.rows.pop(0)

    def add(self, value):
        self.added.append(value)


def factory(session):
    @asynccontextmanager
    async def provide():
        yield session

    return provide


def request() -> ToolExecutionRequest:
    return ToolExecutionRequest(
        tool_name="write_file",
        arguments={"path": "report.txt", "content": "ready"},
        session_id="session-1",
        run_id="run-1",
        tool_call_id="worker:task-1:1:0:0",
        agent_role="worker",
        plan_id="plan-1",
        task_id="task-1",
        operation_key="operation-1",
        execution_id="execution-1",
        fencing_token=4,
        cancellation_epoch=2,
    )


def active_chain(*, fencing_token=4):
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    return [
        SimpleNamespace(
            run_id="run-1",
            session_id="session-1",
            status="running",
            cancellation_epoch=2,
        ),
        SimpleNamespace(
            plan_id="plan-1",
            run_id="run-1",
            status="running",
            active_round_id="round-1",
            current_version=3,
            cancellation_epoch=2,
        ),
        SimpleNamespace(
            round_id="round-1",
            plan_version=3,
            status="running",
        ),
        SimpleNamespace(
            task_id="task-1",
            plan_id="plan-1",
            run_id="run-1",
            active_execution_id="execution-1",
            status="running",
        ),
        SimpleNamespace(
            round_id="round-1",
            task_id="task-1",
            active_execution_id="execution-1",
            status="running",
        ),
        SimpleNamespace(
            execution_id="execution-1",
            task_id="task-1",
            round_id="round-1",
            status="running",
            fencing_token=fencing_token,
            cancellation_epoch=2,
            lease_until=now + timedelta(seconds=10),
        ),
    ]


@pytest.mark.asyncio
async def test_operation_claim_persists_once_and_replays_its_result():
    fixed_now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    first_session = Session([*active_chain(), None])
    repository = PostgresWorkerToolOperationRepository(
        factory(first_session), clock=lambda: fixed_now
    )

    claim = await repository.claim(request(), args_hash="args-hash")

    assert claim.status is ToolOperationClaimStatus.ACQUIRED
    assert len(first_session.executed) == 1
    operation = first_session.added[0]
    assert isinstance(operation, WorkerToolOperationModel)
    assert operation.operation_key == "operation-1"
    assert operation.status == "executing"

    completed = WorkerToolOperationModel(
        operation_key="operation-1",
        run_id="run-1",
        plan_id="plan-1",
        task_id="task-1",
        execution_id="execution-1",
        tool_name="write_file",
        tool_call_id="worker:task-1:1:0:0",
        args_hash="args-hash",
        status="completed",
        result_json={
            "status": "success",
            "output": "created",
            "error": None,
            "duration_ms": 12,
        },
        created_at=fixed_now,
        completed_at=fixed_now,
    )
    replay_session = Session([*active_chain(), completed])
    replay_repository = PostgresWorkerToolOperationRepository(
        factory(replay_session), clock=lambda: fixed_now
    )

    replay = await replay_repository.claim(request(), args_hash="args-hash")

    assert replay.status is ToolOperationClaimStatus.COMPLETED
    assert replay.result == ToolExecutionResult(
        "success", output="created", duration_ms=12
    )
    assert replay_session.added == []


@pytest.mark.asyncio
async def test_operation_claim_rejects_a_stale_fencing_token():
    fixed_now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    session = Session([*active_chain(fencing_token=5)])
    repository = PostgresWorkerToolOperationRepository(
        factory(session), clock=lambda: fixed_now
    )

    claim = await repository.claim(request(), args_hash="args-hash")

    assert claim.status is ToolOperationClaimStatus.REJECTED
    assert claim.reason == "worker_operation_lease_not_active"
    assert session.added == []


@pytest.mark.asyncio
async def test_operation_completion_persists_the_tool_outcome():
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    operation = WorkerToolOperationModel(
        operation_key="operation-1",
        run_id="run-1",
        plan_id="plan-1",
        task_id="task-1",
        execution_id="execution-1",
        tool_name="write_file",
        tool_call_id="worker:task-1:1:0:0",
        args_hash="args-hash",
        status="executing",
        result_json=None,
        created_at=now,
        completed_at=None,
    )
    session = Session([operation])
    repository = PostgresWorkerToolOperationRepository(
        factory(session), clock=lambda: now
    )

    saved = await repository.complete(
        request(),
        args_hash="args-hash",
        result=ToolExecutionResult("success", output="created", duration_ms=12),
    )

    assert saved is True
    assert operation.status == "completed"
    assert operation.result_json == {
        "status": "success",
        "output": "created",
        "error": None,
        "duration_ms": 12,
    }
    assert operation.completed_at == now
