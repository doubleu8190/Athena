from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from application.orchestration.worker_workflow import WorkerWorkflow
from domain.approval import ApprovalDecision, ApprovalStatus
from infrastructure.persistence.postgres.models import (
    AgentPlanModel,
    AgentRunModel,
    ApprovalRecordModel,
    OrchestrationOutboxModel,
    WorkerJobModel,
)
from infrastructure.persistence.postgres.repositories.approval_repository import (
    PostgresApprovalRepository,
)
from workers.approval_expiry_worker import ApprovalExpiryWorker

NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


class Result:
    def __init__(self, rows=(), *, rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def all(self):
        return self._rows


class ExpirySession:
    def __init__(self, run, plan, approval):
        self.run = run
        self.plan = plan
        self.approval = approval
        self.added = []
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return self

    async def execute(self, statement):
        self.statements.append(statement)
        return Result([("approval-1", "run-1", "plan-1")])

    async def get(self, model, _key, **_kwargs):
        if model is AgentRunModel:
            return self.run
        if model is AgentPlanModel:
            return self.plan
        if model is ApprovalRecordModel:
            return self.approval
        return None

    def add(self, value):
        self.added.append(value)


@pytest.mark.asyncio
async def test_expired_approval_is_audited_and_resumes_worker_as_denied():
    run = SimpleNamespace(status="running", run_id="run-1")
    plan = SimpleNamespace(
        run_id="run-1",
        plan_id="plan-1",
        status="running",
        current_version=2,
        active_round_id="round-2",
    )
    approval = SimpleNamespace(
        approval_id="approval-1",
        run_id="run-1",
        plan_id="plan-1",
        task_id="task-1",
        execution_id="execution-1",
        worker_thread_id="task:task-1:exec:1",
        interrupt_id="interrupt-1",
        tool_call_id="call-1",
        tool_name="write_file",
        args_hash="args-hash",
        operation_key="operation-1",
        status="pending",
        decision=None,
        expires_at=NOW - timedelta(seconds=1),
    )
    session = ExpirySession(run, plan, approval)

    @asynccontextmanager
    async def session_factory():
        yield session

    repository = PostgresApprovalRepository(
        session_factory, clock=lambda: NOW
    )

    assert await repository.expire_due_approvals() == 1
    assert approval.status == ApprovalStatus.EXPIRED.value
    assert approval.decision == ApprovalDecision.EXPIRED.value
    outbox = next(
        item for item in session.added if isinstance(item, OrchestrationOutboxModel)
    )
    resume_job = next(
        item for item in session.added if isinstance(item, WorkerJobModel)
    )
    assert outbox.payload_json["decision"] == "expired"
    assert resume_job.payload_json["decision"] == "expired"
    assert resume_job.dedupe_key == "resume-worker:approval-1"

    executed_tools = []

    async def execute_tool(**kwargs):
        executed_tools.append(kwargs)
        return {"status": "success"}

    workflow = WorkerWorkflow(execute_tool=execute_tool)
    resumed = await workflow.complete_tool_approval(
        {
            "execution": {"execution_id": "execution-1"},
            "pending_approval": {
                "approval_id": "approval-1",
                "execution_id": "execution-1",
                "args_hash": "args-hash",
                "operation_key": "operation-1",
                "tool_call_id": "call-1",
                "tool_name": "write_file",
                "arguments": {"path": "report.txt"},
            },
        },
        {
            "approval_id": "approval-1",
            "execution_id": "execution-1",
            "args_hash": "args-hash",
            "operation_key": "operation-1",
            "interrupt_id": "interrupt-1",
            "decision": "expired",
        },
    )
    assert executed_tools == []
    assert (
        resumed["execution"]["tool_results"][0]["error"]["code"]
        == "approval_expired"
    )


@pytest.mark.asyncio
async def test_expiry_worker_repeats_durable_scans_until_stopped():
    second_scan = asyncio.Event()

    class Approvals:
        def __init__(self):
            self.calls = 0

        async def expire_due_approvals(self, *, limit):
            assert limit == 64
            self.calls += 1
            if self.calls >= 2:
                second_scan.set()
            return 0

    approvals = Approvals()
    worker = ApprovalExpiryWorker(approvals, poll_interval=0.01)
    await worker.start()
    await asyncio.wait_for(second_scan.wait(), timeout=1)
    await worker.stop()

    assert approvals.calls >= 2
