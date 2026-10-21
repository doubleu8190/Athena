from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from application.orchestration.langgraph.graphs.worker import (
    build_worker_graph,
    invoke_worker_graph,
)
from application.orchestration.langgraph.runtime.worker_executor import (
    LangGraphWorkerExecutor,
)
from application.orchestration.worker_tools import WorkerToolExecutor
from application.orchestration.worker_workflow import WorkerWorkflow
from application.orchestration.worker_approval import WorkerApprovalService
from application.approval.service import ApprovalService
from domain.orchestration import Task, TaskExecution, TaskStatus, WorkerJobLease
from infrastructure.persistence.postgres.repositories.approval_repository import (
    PostgresApprovalRepository,
)
from infrastructure.persistence.postgres.repositories.worker_job_repository import (
    PostgresWorkerJobRepository,
)
from infrastructure.persistence.postgres.models import OrchestrationOutboxModel
from workers.worker_job_dispatcher import WorkerJobDispatcher


class ToolService:
    def __init__(self):
        self.executed = []

    async def get(self, name):
        return SimpleNamespace(
            name=name,
            enabled=True,
            risk_level="high",
            require_approval=True,
            execution_mode="native",
        )

    async def execute(self, request):
        self.executed.append(request)
        return SimpleNamespace(
            status="success",
            output="wrote file",
            error=None,
            duration_ms=1,
        )


class WorkerAgent:
    def __init__(self):
        self.calls = 0

    async def __call__(self, *, execution, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return {
                "status": "running",
                "tool_calls": [
                    {
                        "name": "write_file",
                        "arguments": {"path": "report.txt", "content": "ready"},
                    }
                ],
            }
        return {"status": "done", "output": {"confirmed": True}}


class RepositorySession:
    def __init__(self, approval):
        self.approval = approval
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return self

    async def get(self, model, _key, **_kwargs):
        if model.__name__ == "AgentRunModel":
            return SimpleNamespace(status="running")
        if model.__name__ == "AgentPlanModel":
            return SimpleNamespace(
                plan_id=self.approval.plan_id,
                run_id=self.approval.run_id,
                status="running",
                current_version=1,
                active_round_id="round-1",
            )
        return self.approval

    async def execute(self, _statement):
        return SimpleNamespace(rowcount=1)

    def add(self, value):
        self.added.append(value)


class Events:
    def __init__(self):
        self.values = []

    async def publish(self, event):
        self.values.append(event)


@pytest.mark.asyncio
async def test_high_risk_worker_tool_interrupts_and_executes_same_arguments_after_approval():
    tools = ToolService()
    tool_executor = WorkerToolExecutor(tools)
    workflow = WorkerWorkflow(
        execute=WorkerAgent(),
        execute_tool=tool_executor,
    )
    graph = build_worker_graph(
        workflow=workflow,
        checkpointer=InMemorySaver(),
    )
    execution = {
        "plan_id": "plan-1",
        "run_id": "run-1",
        "session_id": "session-1",
        "execution_id": "execution-1",
        "worker_thread_id": "task:task-1:exec:1",
        "execution_generation": 1,
        "task": {
            "task_id": "task-1",
            "allowed_tools": ["write_file"],
            "objective": "write a report",
        },
    }

    paused = await invoke_worker_graph(
        graph,
        task_id="task-1",
        execution_generation=1,
        execution=execution,
    )

    interrupts = paused["__interrupt__"]
    assert len(interrupts) == 1
    approval = dict(interrupts[0].value)
    assert approval["tool_name"] == "write_file"
    assert approval["arguments"] == {"path": "report.txt", "content": "ready"}
    assert approval["operation_key"] == "athena:run-1:task-1:1:0:0"
    assert tools.executed == []

    finished = await invoke_worker_graph(
        graph,
        task_id="task-1",
        execution_generation=1,
        execution=execution,
        resume_value={
            **approval,
            "interrupt_id": interrupts[0].id,
            "decision": "approved",
        },
    )

    assert finished["status"] == "done"
    assert finished["result"]["output"] == {"confirmed": True}
    assert len(tools.executed) == 1
    assert tools.executed[0].arguments == {
        "path": "report.txt",
        "content": "ready",
    }
    assert tools.executed[0].approval_decision == "approved"
    assert tools.executed[0].operation_key == approval["operation_key"]


@pytest.mark.asyncio
async def test_worker_approval_request_persists_interrupt_identity_and_notifies():
    class Jobs:
        def __init__(self):
            self.request = None

        async def wait_for_approval(self, lease, approval):
            self.request = (lease, approval)
            return True

    jobs = Jobs()
    lease = WorkerJobLease(
        job_id="job-1",
        worker_id="dispatcher-1",
        run_id="run-1",
        plan_id="plan-1",
        round_id="round-1",
        plan_version=1,
        task_id="task-1",
        execution_id="execution-1",
        session_id="session-1",
        attempt=1,
        worker_thread_id="task:task-1:exec:1",
        fencing_token=3,
        cancellation_epoch=0,
        lease_until=SimpleNamespace(),
        task=Task("task-1", "write", "write a file"),
    )
    request = {
        "approval_id": "approval-1",
        "interrupt_id": "interrupt-1",
        "run_id": "run-1",
        "plan_id": "plan-1",
        "task_id": "task-1",
        "execution_id": "execution-1",
        "worker_thread_id": "task:task-1:exec:1",
        "tool_call_id": "worker:task-1:1:0:0",
        "tool_name": "write_file",
        "arguments": {"path": "report.txt", "content": "ready"},
        "risk_level": "high",
        "args_hash": "args-hash",
        "operation_key": "athena:run-1:task-1:1:0:0",
    }

    assert await WorkerApprovalService(jobs).request(lease, request)
    assert jobs.request[1]["interrupt_id"] == "interrupt-1"


@pytest.mark.asyncio
async def test_resolving_worker_approval_enqueues_resume_job_in_same_repository_call():
    approval = SimpleNamespace(
        approval_id="approval-1",
        run_id="run-1",
        plan_id="plan-1",
        task_id="task-1",
        tool_call_id="call-1",
        tool_name="write_file",
        status="pending",
        execution_id="execution-1",
        worker_thread_id="task:task-1:exec:1",
        interrupt_id="interrupt-1",
        args_hash="args-hash",
        operation_key="operation-1",
    )
    session = RepositorySession(approval)

    class Factory:
        def __call__(self):
            class Context:
                async def __aenter__(self):
                    return session

                async def __aexit__(self, *_args):
                    return False

            return Context()

    repository = PostgresApprovalRepository(Factory())
    from domain.approval import ApprovalDecision

    assert await repository.resolve(
        "approval-1",
        run_id="run-1",
        task_id="task-1",
        decision=ApprovalDecision.APPROVED,
    )
    resume_job = next(
        item
        for item in session.added
        if getattr(item, "job_type", None) == "resume_worker"
    )
    outbox = next(
        item for item in session.added if isinstance(item, OrchestrationOutboxModel)
    )
    assert resume_job.approval_id == "approval-1"
    assert resume_job.payload_json["interrupt_id"] == "interrupt-1"
    assert outbox.event_type == "ApprovalResolved"
    assert outbox.dedupe_key == "approval:approval-1:resolved"
    assert outbox.payload_json["decision"] == "approved"


@pytest.mark.asyncio
async def test_worker_approval_request_writes_outbox_in_execution_transaction(
    monkeypatch,
):
    class Session:
        def __init__(self):
            self.added = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def begin(self):
            return self

        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, value):
            self.added.append(value)

    session = Session()
    lease = WorkerJobLease(
        job_id="job-1",
        worker_id="dispatcher-1",
        run_id="run-1",
        plan_id="plan-1",
        round_id="round-1",
        plan_version=1,
        task_id="task-1",
        execution_id="execution-1",
        session_id="session-1",
        attempt=1,
        worker_thread_id="task:task-1:exec:1",
        fencing_token=3,
        cancellation_epoch=0,
        lease_until=SimpleNamespace(),
        task=Task("task-1", "write", "write a file"),
    )
    run = SimpleNamespace(run_id="run-1", session_id="session-1", status="running", cancellation_epoch=0)
    plan = SimpleNamespace(plan_id="plan-1", run_id="run-1", status="running", active_round_id="round-1", current_version=1, cancellation_epoch=0)
    round_row = SimpleNamespace(round_id="round-1", plan_version=1, status="running")
    task = SimpleNamespace(task_id="task-1", plan_id="plan-1", run_id="run-1", status="running", active_execution_id="execution-1", updated_at=None)
    round_task = SimpleNamespace(status="running", active_execution_id="execution-1", terminal_at=None)
    execution = SimpleNamespace(execution_id="execution-1", worker_thread_id="task:task-1:exec:1", status="running", cancellation_epoch=0, lease_owner="dispatcher-1", fencing_token=3, task_id="task-1", round_id="round-1", updated_at=None)
    job = SimpleNamespace(job_type="execute", status="running", lease_owner="dispatcher-1", execution_id="execution-1", updated_at=None)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def provide_session():
        yield session

    repository = PostgresWorkerJobRepository(
        provide_session, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    )

    async def lock_chain(_session, _lease):
        return run, plan, round_row, task, round_task, execution, job

    monkeypatch.setattr(repository, "_lock_execution_chain", lock_chain)
    approval = {
        "approval_id": "approval-1",
        "interrupt_id": "interrupt-1",
        "run_id": "run-1",
        "plan_id": "plan-1",
        "task_id": "task-1",
        "execution_id": "execution-1",
        "worker_thread_id": "task:task-1:exec:1",
        "tool_call_id": "call-1",
        "tool_name": "write_file",
        "arguments": {"path": "report.txt"},
        "args_hash": "args-hash",
        "operation_key": "operation-1",
    }

    assert await repository.wait_for_approval(lease, approval)
    outbox = next(
        item for item in session.added if isinstance(item, OrchestrationOutboxModel)
    )
    assert outbox.event_type == "ApprovalRequested"
    assert outbox.dedupe_key == "approval:approval-1:requested"
    assert outbox.payload_json["tool_name"] == "write_file"
    assert task.status == "waiting_approval"
    assert execution.status == "waiting_approval"


@pytest.mark.asyncio
async def test_tool_results_before_approval_are_checkpointed_for_resume():
    async def execute_tool(*, tool_call, **_kwargs):
        if tool_call["name"] == "read_file":
            return {"id": "read-1", "status": "success", "output": "contents"}
        return {
            "status": "approval_required",
            "approval_request": {"approval_id": "approval-1"},
        }

    workflow = WorkerWorkflow(execute_tool=execute_tool)
    patch = await workflow.execute_tool_batch(
        {
            "execution": {"tool_results": []},
            "tool_round": 2,
            "result": {
                "tool_calls": [
                    {"name": "read_file", "arguments": {"path": "a.txt"}},
                    {"name": "write_file", "arguments": {"path": "b.txt"}},
                ]
            },
        }
    )

    assert patch["waiting"] is True
    assert patch["pending_approval"]["approval_id"] == "approval-1"
    assert patch["tool_round"] == 3
    assert patch["execution"]["tool_results"][0]["output"] == "contents"


@pytest.mark.asyncio
async def test_worker_batch_approval_does_not_resume_root_run():
    worker_approval = SimpleNamespace(
        approval_id="approval-1",
        execution_id="execution-1",
        session_id="session-1",
        run_id="run-1",
        task_id="task-1",
    )

    class ApprovalRepo:
        async def resolve_batch(self, *_args, **_kwargs):
            return True

        async def get(self, _approval_id):
            return worker_approval

    root_resumes = []
    events = Events()
    service = ApprovalService(
        ApprovalRepo(),
        events,
        id_factory=lambda: "unused",
        resumer=lambda *args: root_resumes.append(args),
    )

    assert await service.resolve_batch(
        "batch-1", {"approval-1": "approved"}, run_id="run-1"
    )
    assert root_resumes == []
    assert len(events.values) == 1
    assert events.values[0].event_type.value == "approval.resolved"


@pytest.mark.asyncio
async def test_task_approval_resolution_uses_transactional_outbox():
    approval = SimpleNamespace(
        approval_id="approval-1",
        session_id="session-1",
        run_id="run-1",
        plan_id="plan-1",
        task_id="task-1",
        execution_id="execution-1",
    )

    class ApprovalRepo:
        async def get(self, _approval_id):
            return approval

        async def resolve(self, *_args, **_kwargs):
            return True

    events = Events()
    service = ApprovalService(ApprovalRepo(), events, id_factory=lambda: "unused")

    assert await service.resolve(
        "approval-1",
        run_id="run-1",
        task_id="task-1",
        decision="approved",
    )
    assert events.values == []


@pytest.mark.asyncio
async def test_worker_executor_recovers_checkpointed_interrupt_after_process_restart():
    tools = ToolService()
    executor = LangGraphWorkerExecutor(
        workflow_factory=lambda: WorkerWorkflow(
            execute=WorkerAgent(),
            execute_tool=WorkerToolExecutor(tools),
        ),
        checkpointer=InMemorySaver(),
    )
    execution = TaskExecution(
        plan_id="plan-1",
        run_id="run-1",
        session_id="session-1",
        task=Task(
            "task-1",
            "write",
            "write a report",
            allowed_tools=("write_file",),
        ),
        execution_generation=1,
        execution_id="execution-1",
        worker_thread_id="task:task-1:exec:1",
    )

    result = await executor.execute(execution)
    recovered = await executor.pending_interrupt(execution)

    assert result.status == TaskStatus.WAITING_APPROVAL
    assert recovered is not None
    assert recovered["interrupt_id"]
    assert recovered["execution_id"] == "execution-1"
    assert recovered["operation_key"] == "athena:run-1:task-1:1:0:0"
    assert tools.executed == []


@pytest.mark.asyncio
async def test_reclaimed_worker_job_registers_checkpointed_approval_without_reinvoking_agent():
    class Jobs:
        async def renew_lease(self, lease):
            return lease

    class Worker:
        async def pending_interrupt(self, _execution):
            return {
                "approval_id": "approval-1",
                "interrupt_id": "interrupt-1",
                "run_id": "run-1",
                "plan_id": "plan-1",
                "task_id": "task-1",
                "execution_id": "execution-1",
                "worker_thread_id": "task:task-1:exec:1",
                "tool_call_id": "worker:task-1:1:0:0",
                "tool_name": "write_file",
                "arguments": {"path": "report.txt"},
                "risk_level": "high",
                "args_hash": "args-hash",
                "operation_key": "operation-1",
            }

        async def execute(self, _execution):
            raise AssertionError("reclaimed interrupt must not invoke the agent")

    class ApprovalService:
        def __init__(self):
            self.request_value = None

        async def request(self, lease, approval):
            self.request_value = (lease, approval)
            return True

    class EventHandler:
        async def apply_terminal_event(self, _event):
            raise AssertionError("approval wait is not a terminal Task event")

    approvals = ApprovalService()
    dispatcher = WorkerJobDispatcher(
        Jobs(),
        Worker(),
        EventHandler(),
        worker_id="dispatcher-1",
        lease_seconds=10,
        max_concurrency=1,
        approval_service=approvals,
    )
    lease = WorkerJobLease(
        job_id="job-1",
        worker_id="dispatcher-1",
        run_id="run-1",
        plan_id="plan-1",
        round_id="round-1",
        plan_version=1,
        task_id="task-1",
        execution_id="execution-1",
        session_id="session-1",
        attempt=1,
        worker_thread_id="task:task-1:exec:1",
        fencing_token=2,
        cancellation_epoch=0,
        lease_until=datetime.now(timezone.utc),
        task=Task("task-1", "write", "write a report"),
    )

    await dispatcher._execute(lease)

    assert approvals.request_value[1]["interrupt_id"] == "interrupt-1"
    assert approvals.request_value[1]["execution_id"] == "execution-1"
