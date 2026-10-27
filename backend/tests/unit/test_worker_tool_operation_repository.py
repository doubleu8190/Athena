from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from domain.tools import (
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolOperationClaimStatus,
)
from domain.leases import LeaseLostError
from tests.fakes.leases import LeaseCenter, lease_grant
from infrastructure.persistence.postgres.models import WorkerToolOperationModel
from infrastructure.persistence.postgres.repositories.worker_tool_operation_repository import (
    PostgresWorkerToolOperationRepository,
)


class Session:
    def __init__(self, rows):
        self.rows = list(rows)
        self.operation_rows = [
            row for row in rows if isinstance(row, WorkerToolOperationModel)
        ]
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

    async def scalars(self, _statement):
        return SimpleNamespace(all=lambda: self.operation_rows)

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
        logical_operation_key="athena:run-1:task-1:logical:0:0",
        execution_id="execution-1",
        lease=lease_grant("worker_execution", "execution-1", fencing_token=4),
        cancellation_epoch=2,
    )


def active_chain(*, execution_id="execution-1"):
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
            active_execution_id=execution_id,
            status="running",
        ),
        SimpleNamespace(
            round_id="round-1",
            task_id="task-1",
            active_execution_id=execution_id,
            status="running",
        ),
        SimpleNamespace(
            execution_id=execution_id,
            task_id="task-1",
            round_id="round-1",
            status="running",
            cancellation_epoch=2,
        ),
    ]


@pytest.mark.asyncio
async def test_operation_claim_persists_once_and_replays_its_result():
    fixed_now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    first_session = Session([*active_chain(), None])
    repository = PostgresWorkerToolOperationRepository(
        factory(first_session), clock=lambda: fixed_now, lease_center=LeaseCenter()
    )

    claim = await repository.claim(request(), args_hash="args-hash")

    assert claim.status is ToolOperationClaimStatus.ACQUIRED
    assert len(first_session.executed) == 1
    operation = first_session.added[0]
    assert isinstance(operation, WorkerToolOperationModel)
    assert operation.operation_key == "operation-1"
    assert operation.logical_operation_key == request().logical_operation_key
    assert operation.status == "executing"

    completed = WorkerToolOperationModel(
        operation_key="operation-1",
        logical_operation_key="athena:run-1:task-1:logical:0:0",
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
        factory(replay_session), clock=lambda: fixed_now, lease_center=LeaseCenter()
    )

    replay = await replay_repository.claim(request(), args_hash="args-hash")

    assert replay.status is ToolOperationClaimStatus.COMPLETED
    assert replay.result == ToolExecutionResult(
        "success", output="created", duration_ms=12
    )
    assert replay_session.added == []


@pytest.mark.asyncio
async def test_operation_claim_replays_completed_effect_across_execution_attempts():
    fixed_now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    first_attempt = WorkerToolOperationModel(
        operation_key="operation-1",
        logical_operation_key="athena:run-1:task-1:logical:0:0",
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
    retry = replace(
        request(),
        operation_key="operation-2",
        tool_call_id="worker:task-1:2:0:0",
        execution_id="execution-2",
        lease=lease_grant("worker_execution", "execution-2", fencing_token=5),
    )
    session = Session([*active_chain(execution_id="execution-2"), None])
    session.operation_rows = [first_attempt]
    repository = PostgresWorkerToolOperationRepository(
        factory(session), clock=lambda: fixed_now, lease_center=LeaseCenter()
    )

    claim = await repository.claim(retry, args_hash="args-hash")

    assert claim.status is ToolOperationClaimStatus.COMPLETED
    assert claim.result == ToolExecutionResult(
        "success", output="created", duration_ms=12
    )
    assert session.added == []


@pytest.mark.asyncio
async def test_operation_claim_rejects_changed_arguments_for_a_logical_call():
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    previous = WorkerToolOperationModel(
        operation_key="operation-1",
        logical_operation_key="athena:run-1:task-1:logical:0:0",
        run_id="run-1",
        plan_id="plan-1",
        task_id="task-1",
        execution_id="execution-1",
        tool_name="write_file",
        tool_call_id="worker:task-1:1:0:0",
        args_hash="old-args-hash",
        status="completed",
        result_json={"status": "success", "output": "created"},
        created_at=now,
        completed_at=now,
    )
    session = Session([*active_chain(), None])
    session.operation_rows = [previous]
    repository = PostgresWorkerToolOperationRepository(
        factory(session), clock=lambda: now, lease_center=LeaseCenter()
    )

    claim = await repository.claim(request(), args_hash="new-args-hash")

    assert claim.status is ToolOperationClaimStatus.CONFLICT
    assert claim.reason == "worker_logical_operation_conflict"
    assert session.added == []


@pytest.mark.asyncio
async def test_operation_claim_fails_closed_when_prior_attempt_is_incomplete():
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    previous = WorkerToolOperationModel(
        operation_key="operation-1",
        logical_operation_key="athena:run-1:task-1:logical:0:0",
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
    session = Session([*active_chain(execution_id="execution-2"), None])
    session.operation_rows = [previous]
    retry = replace(
        request(),
        operation_key="operation-2",
        tool_call_id="worker:task-1:2:0:0",
        execution_id="execution-2",
        lease=lease_grant("worker_execution", "execution-2", fencing_token=5),
    )
    repository = PostgresWorkerToolOperationRepository(
        factory(session), clock=lambda: now, lease_center=LeaseCenter()
    )

    claim = await repository.claim(retry, args_hash="args-hash")

    assert claim.status is ToolOperationClaimStatus.IN_PROGRESS
    assert claim.reason == "worker_operation_outcome_unknown"
    assert session.added == []


@pytest.mark.asyncio
async def test_operation_claim_rejects_a_stale_fencing_token():
    fixed_now = datetime(2026, 10, 22, tzinfo=timezone.utc)

    class StaleLeaseCenter(LeaseCenter):
        async def assert_current_in_transaction(self, _session, grant):
            raise LeaseLostError(grant.key)

    session = Session([*active_chain()])
    repository = PostgresWorkerToolOperationRepository(
        factory(session), clock=lambda: fixed_now, lease_center=StaleLeaseCenter()
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
        logical_operation_key="athena:run-1:task-1:logical:0:0",
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
        factory(session), clock=lambda: now, lease_center=LeaseCenter()
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
