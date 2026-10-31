from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from domain.approval import ApprovalDecision
from application.orchestration.langgraph.graphs.worker import (
    build_worker_graph,
    invoke_worker_graph as _invoke_worker_graph,
)
from application.orchestration.langgraph.runtime.worker_executor import (
    LangGraphWorkerExecutor,
)
from application.orchestration.worker_tools import WorkerToolExecutor, worker_tool_manifest
from application.orchestration.root_decision import RootAgentInput, RootMessage
from application.orchestration.worker_workflow import WorkerWorkflow
from application.orchestration.worker_approval import WorkerApprovalService
from domain.orchestration import (
    Task,
    TaskExecution,
    TaskResult,
    TaskStatus,
    WorkerJobLease,
)
from domain.tools import (
    ToolExecutionResult,
    ToolOperationClaim,
    ToolOperationClaimStatus,
)
from infrastructure.persistence.postgres.repositories.approval_repository import (
    PostgresApprovalRepository,
)
from infrastructure.persistence.postgres.repositories.worker_job_repository import (
    PostgresWorkerJobRepository,
)
from tests.fakes.worker_runtime import (
    AcceptingWorkerApprovalService,
    NoopGraphThreadLock,
)
from tests.fakes.leases import LeaseCenter, lease_context, lease_grant
from infrastructure.persistence.postgres.models import OrchestrationOutboxModel
from workers.worker_job_dispatcher import WorkerJobDispatcher


async def invoke_worker_graph(graph, *, execution, **kwargs):
    """Attach a synthetic worker grant to direct graph unit invocations."""
    execution = dict(execution)
    execution.setdefault(
        "lease",
        lease_context(
            lease_grant(
                "worker_execution",
                str(execution["execution_id"]),
                fencing_token=int(execution.get("fencing_token", 1)),
            )
        ),
    )
    return await _invoke_worker_graph(graph, execution=execution, **kwargs)


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


@pytest.mark.asyncio
async def test_worker_manifest_is_valid_root_input():
    class Tools:
        async def list_tools(self):
            return [SimpleNamespace(
                name="read_file", description="Read a file", parameters={},
                enabled=True, risk_level="low", require_approval=False,
                execution_mode="native",
            )]

    manifest = await worker_tool_manifest(Tools())
    root_input = RootAgentInput(
        session_id="session", run_id="run", message_id="message",
        messages=(RootMessage(role="user", content="hello"),),
        worker_tools=manifest, remaining_turns=1,
    )
    assert root_input.worker_tools[0].name == "read_file"

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
        return {"status": "completed", "output": {"confirmed": True}}


def test_worker_executor_rejects_legacy_done_status():
    execution = TaskExecution(
        plan_id="plan-1",
        run_id="run-1",
        session_id="session-1",
        task=Task("task-1", "test", "test legacy status"),
        execution_generation=1,
    )

    result = LangGraphWorkerExecutor._result(execution, {
        "status": "done",
        "result": {"output": {"confirmed": True}},
    })

    assert result.status is TaskStatus.FAILED


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

    assert finished["status"] == "completed"
    assert finished["result"]["output"] == {"confirmed": True}
    assert len(tools.executed) == 1
    assert tools.executed[0].arguments == {
        "path": "report.txt",
        "content": "ready",
    }
    assert tools.executed[0].approval_decision == "approved"
    assert tools.executed[0].operation_key == approval["operation_key"]


@pytest.mark.asyncio
async def test_approval_resume_uses_refreshed_worker_lease_fence():
    class FenceLedger:
        def __init__(self):
            self.fencing_tokens = []

        async def claim(self, _request, *, args_hash):
            token = _request.lease.fencing_token
            self.fencing_tokens.append(token)
            if token != 8:
                return ToolOperationClaim(
                    ToolOperationClaimStatus.REJECTED,
                    reason="worker_operation_lease_not_active",
                )
            return ToolOperationClaim(ToolOperationClaimStatus.ACQUIRED)

        async def complete(self, *_args, **_kwargs):
            return True

    tools = ToolService()
    ledger = FenceLedger()
    executor = WorkerToolExecutor(tools, operation_ledger=ledger)
    workflow = WorkerWorkflow(execute=WorkerAgent(), execute_tool=executor)
    graph = build_worker_graph(workflow=workflow, checkpointer=InMemorySaver())
    initial_execution = {
        "plan_id": "plan-1",
        "run_id": "run-1",
        "session_id": "session-1",
        "execution_id": "execution-1",
        "worker_thread_id": "task:task-1:exec:1",
        "execution_generation": 1,
        "fencing_token": 7,
        "cancellation_epoch": 0,
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
        execution=initial_execution,
    )
    interrupt = paused["__interrupt__"][0]
    approval = dict(interrupt.value)
    refreshed_execution = {**initial_execution, "fencing_token": 8}

    finished = await invoke_worker_graph(
        graph,
        task_id="task-1",
        execution_generation=1,
        execution=refreshed_execution,
        resume_value={
            **approval,
            "interrupt_id": interrupt.id,
            "decision": "approved",
        },
    )

    assert ledger.fencing_tokens == [8]
    assert len(tools.executed) == 1
    assert tools.executed[0].lease.fencing_token == 8
    assert finished["status"] == "completed"


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
        lease=lease_grant("worker_execution", "execution-1", fencing_token=3),
        cancellation_epoch=0,
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
async def test_legacy_worker_approval_resume_is_disabled_during_s2():
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
        expires_at=None,
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
    with pytest.raises(
        RuntimeError,
        match="legacy_worker_approval_resume_disabled_during_worker_s2",
    ):
        await repository.resolve(
            "approval-1",
            decision=ApprovalDecision.APPROVED,
        )
    assert session.added == []


@pytest.mark.parametrize("job_type", ["execute", "resume_worker"])
@pytest.mark.asyncio
async def test_worker_approval_request_writes_outbox_in_execution_transaction(
    monkeypatch, job_type,
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
        lease=lease_grant("worker_execution", "execution-1", fencing_token=3),
        cancellation_epoch=0,
        task=Task("task-1", "write", "write a file"),
        job_type=job_type,
    )
    run = SimpleNamespace(run_id="run-1", session_id="session-1", status="running", cancellation_epoch=0)
    plan = SimpleNamespace(plan_id="plan-1", run_id="run-1", status="running", active_round_id="round-1", current_version=1, cancellation_epoch=0)
    round_row = SimpleNamespace(round_id="round-1", plan_version=1, status="running")
    task = SimpleNamespace(task_id="task-1", plan_id="plan-1", run_id="run-1", status="running", active_execution_id="execution-1", updated_at=None)
    round_task = SimpleNamespace(status="running", active_execution_id="execution-1", terminal_at=None)
    execution = SimpleNamespace(execution_id="execution-1", worker_thread_id="task:task-1:exec:1", status="running", cancellation_epoch=0, lease_owner="dispatcher-1", fencing_token=3, task_id="task-1", round_id="round-1", updated_at=None)
    job = SimpleNamespace(job_type=job_type, status="running", lease_owner="dispatcher-1", execution_id="execution-1", updated_at=None)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def provide_session():
        yield session

    repository = PostgresWorkerJobRepository(
        provide_session,
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        lease_center=LeaseCenter(),
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
    assert patch["tool_round"] == 2
    assert patch["execution"]["tool_results"][0]["output"] == "contents"


@pytest.mark.asyncio
async def test_batch_continues_across_two_approvals_with_original_call_indices():
    class BatchAgent:
        def __init__(self):
            self.calls = 0
            self.results = None

        async def __call__(self, *, execution, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "status": "running",
                    "tool_calls": [
                        {"name": "write_file", "arguments": {"path": str(i)}}
                        for i in range(3)
                    ],
                }
            self.results = execution["tool_results"]
            return {"status": "completed"}

    agent = BatchAgent()
    tools = ToolService()
    executor = LangGraphWorkerExecutor(
        workflow_factory=lambda: WorkerWorkflow(
            execute=agent, execute_tool=WorkerToolExecutor(tools)
        ),
        checkpointer=InMemorySaver(),
    )
    execution = TaskExecution(
        plan_id="plan-1", run_id="run-1", session_id="session-1",
        task=Task("task-1", "write", "write files", allowed_tools=("write_file",)),
        execution_generation=1, execution_id="execution-1",
        worker_thread_id="task:task-1:exec:1",
        lease=lease_grant("worker_execution", "execution-1"),
    )
    result = await executor.execute(execution)
    for index, decision in enumerate(("approved", "denied", "approved")):
        assert result.status is TaskStatus.WAITING_APPROVAL
        approval = await executor.pending_interrupt(execution)
        assert approval is not None
        assert approval["call_index"] == index
        assert approval["tool_round"] == 0
        result = await executor.resume(execution, {**approval, "decision": decision})
        if index == 0:
            new_interrupt = await executor.pending_interrupt(execution)
            lease = WorkerJobLease(
                job_id="resume-1", worker_id="worker", run_id="run-1",
                plan_id="plan-1", round_id="round-1", plan_version=1,
                task_id="task-1", execution_id="execution-1", session_id="session-1",
                attempt=1, worker_thread_id="task:task-1:exec:1",
                lease=execution.lease, cancellation_epoch=0, task=execution.task,
                job_type="resume_worker", resume_value={**approval, "decision": decision},
            )
            dispatcher = WorkerJobDispatcher(
                None, executor, None, worker_id="worker", lease_center=LeaseCenter(),
                lease_seconds=10, max_concurrency=1, approval_service=None,
                thread_lock=NoopGraphThreadLock(),
            )
            recovered = await dispatcher._invoke_worker(lease, execution)
            assert recovered.status is TaskStatus.WAITING_APPROVAL
            assert recovered.approval_batch == new_interrupt
    assert result.status is TaskStatus.COMPLETED
    assert agent.calls == 2
    assert [item["status"] for item in agent.results] == ["success", "denied", "success"]
    assert [request.operation_key for request in tools.executed] == [
        "athena:run-1:task-1:1:0:0",
        "athena:run-1:task-1:1:0:2",
    ]


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
        async def renew_lease(self, lease, *, lease_seconds):
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

        async def resume(self, _execution, _resume_value):
            raise AssertionError("execute job must not resume the Worker")

    class ApprovalService:
        def __init__(self):
            self.request_value = None

        async def request(self, lease, approval):
            self.request_value = (lease, approval)
            return True

    class EventHandler:
        async def apply_worker_terminal_event(self, _event):
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
        thread_lock=NoopGraphThreadLock(),
        lease_center=LeaseCenter(),
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
        lease=lease_grant("worker_execution", "execution-1", fencing_token=2),
        cancellation_epoch=0,
        task=Task("task-1", "write", "write a report"),
    )

    await dispatcher._execute(lease)

    assert approvals.request_value[1]["interrupt_id"] == "interrupt-1"
    assert approvals.request_value[1]["execution_id"] == "execution-1"


@pytest.mark.asyncio
async def test_resume_worker_job_resumes_same_checkpoint_and_reports_task_terminal():
    class Jobs:
        async def renew_lease(self, lease, *, lease_seconds):
            return lease

    class Worker:
        def __init__(self):
            self.resumed = None

        async def resume(self, execution, resume_value):
            self.resumed = (execution, resume_value)
            return TaskResult(
                task_id="task-1",
                status=TaskStatus.COMPLETED,
                output={"saved": True},
            )

        async def execute(self, _execution):
            raise AssertionError("resume job must continue the saved checkpoint")

        async def pending_interrupt(self, _execution):
            return None

    class EventHandler:
        def __init__(self):
            self.events = []

        async def apply_worker_terminal_event(self, event):
            self.events.append(event)
            return SimpleNamespace(accepted=True, reason=None)

    worker = Worker()
    events = EventHandler()
    dispatcher = WorkerJobDispatcher(
        Jobs(),
        worker,
        events,
        worker_id="dispatcher-1",
        lease_seconds=10,
        max_concurrency=1,
        approval_service=AcceptingWorkerApprovalService(),
        thread_lock=NoopGraphThreadLock(),
        lease_center=LeaseCenter(),
        id_factory=lambda: "terminal-event-1",
        clock=lambda: datetime(2026, 10, 22, tzinfo=timezone.utc),
    )
    resume_value = {
        "approval_id": "approval-1",
        "decision": "approved",
        "interrupt_id": "interrupt-1",
        "execution_id": "execution-1",
        "args_hash": "args-hash",
        "operation_key": "operation-1",
    }
    lease = WorkerJobLease(
        job_id="resume-job-1",
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
        lease=lease_grant("worker_execution", "execution-1", fencing_token=4),
        cancellation_epoch=0,
        task=Task("task-1", "write", "write a report"),
        job_type="resume_worker",
        approval_id="approval-1",
        resume_value=resume_value,
    )

    await dispatcher._execute(lease)

    assert worker.resumed is not None
    execution, actual_resume_value = worker.resumed
    assert execution.execution_id == "execution-1"
    assert execution.worker_thread_id == "task:task-1:exec:1"
    assert actual_resume_value == resume_value
    assert len(events.events) == 1
    assert events.events[0].message_id == "terminal-event-1"
    assert events.events[0].status is TaskStatus.COMPLETED
    assert events.events[0].result["output"] == {"saved": True}


@pytest.mark.asyncio
async def test_worker_tool_operation_ledger_replays_completed_result_without_reexecution():
    class LowRiskToolService:
        def __init__(self):
            self.executed = []

        async def get(self, name):
            return SimpleNamespace(
                name=name,
                enabled=True,
                risk_level="low",
                require_approval=False,
                execution_mode="native",
            )

        async def execute(self, request):
            self.executed.append(request)
            return ToolExecutionResult("success", output="created")

    class Ledger:
        def __init__(self):
            self.result = None
            self.completed = []
            self.claimed = []

        async def claim(self, request, *, args_hash):
            self.claimed.append(request)
            self.args_hash = args_hash
            if self.result is not None:
                return ToolOperationClaim(
                    ToolOperationClaimStatus.COMPLETED, result=self.result
                )
            return ToolOperationClaim(ToolOperationClaimStatus.ACQUIRED)

        async def complete(self, _request, *, args_hash, result):
            assert args_hash == self.args_hash
            self.result = result
            self.completed.append(result)
            return True

    tools = LowRiskToolService()
    ledger = Ledger()
    executor = WorkerToolExecutor(tools, operation_ledger=ledger)
    call = {
        "name": "write_file",
        "arguments": {"path": "report.txt", "content": "ready"},
    }
    execution = {
        "plan_id": "plan-1",
        "run_id": "run-1",
        "session_id": "session-1",
        "task": {"task_id": "task-1", "allowed_tools": ["write_file"]},
        "execution_id": "execution-1",
        "fencing_token": 3,
        "lease": lease_context(lease_grant("worker_execution", "execution-1", fencing_token=3)),
        "cancellation_epoch": 0,
        "execution_generation": 1,
    }

    first = await executor(
        tool_call=call, execution=execution, tool_round=0, call_index=0
    )
    replay = await executor(
        tool_call=call, execution=execution, tool_round=0, call_index=0
    )
    retry_execution = {
        **execution,
        "execution_id": "execution-2",
        "fencing_token": 4,
        "lease": lease_context(lease_grant("worker_execution", "execution-2", fencing_token=4)),
        "execution_generation": 2,
    }
    retry_replay = await executor(
        tool_call=call,
        execution=retry_execution,
        tool_round=0,
        call_index=0,
    )

    assert first == replay
    assert retry_replay["status"] == "success"
    assert retry_replay["output"] == "created"
    assert retry_replay["id"] != first["id"]
    assert first["status"] == "success"
    assert first["output"] == "created"
    assert len(tools.executed) == 1
    assert len(ledger.completed) == 1
    assert ledger.claimed[0].operation_key != ledger.claimed[2].operation_key
    assert (
        ledger.claimed[0].logical_operation_key
        == ledger.claimed[2].logical_operation_key
    )
    assert tools.executed[0].operation_key
    assert tools.executed[0].lease.fencing_token == 3


@pytest.mark.asyncio
async def test_worker_tool_operation_with_unknown_prior_outcome_fails_closed():
    class LowRiskToolService:
        def __init__(self):
            self.executed = []

        async def get(self, name):
            return SimpleNamespace(
                name=name,
                enabled=True,
                risk_level="low",
                require_approval=False,
                execution_mode="native",
            )

        async def execute(self, request):
            self.executed.append(request)
            raise AssertionError("uncertain operations must never be replayed")

    class Ledger:
        async def claim(self, _request, *, args_hash):
            return ToolOperationClaim(
                ToolOperationClaimStatus.IN_PROGRESS,
                reason="worker_operation_outcome_unknown",
            )

        async def complete(self, *_args, **_kwargs):
            raise AssertionError("an unclaimed operation cannot be completed")

    tools = LowRiskToolService()
    executor = WorkerToolExecutor(tools, operation_ledger=Ledger())
    result = await executor(
        tool_call={
            "name": "write_file",
            "arguments": {"path": "report.txt", "content": "ready"},
        },
        execution={
            "plan_id": "plan-1",
            "run_id": "run-1",
            "session_id": "session-1",
            "task": {"task_id": "task-1", "allowed_tools": ["write_file"]},
            "execution_id": "execution-1",
            "fencing_token": 3,
            "lease": lease_context(lease_grant("worker_execution", "execution-1", fencing_token=3)),
            "cancellation_epoch": 0,
            "execution_generation": 1,
        },
        tool_round=0,
        call_index=0,
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "worker_tool_operation_outcome_unknown"
    assert tools.executed == []
