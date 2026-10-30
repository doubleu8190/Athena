"""PostgreSQL acceptance for Worker approval checkpoint recovery."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from langgraph.errors import NodeCancelledError
from psycopg.conninfo import make_conninfo
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.orchestration.langgraph.runtime.worker_executor import (
    LangGraphWorkerExecutor,
)
from application.orchestration.langgraph.graphs.worker import (
    build_worker_graph,
    invoke_worker_graph,
)
from application.orchestration.worker_tools import WorkerToolExecutor
from application.orchestration.worker_workflow import WorkerWorkflow
from domain.approval import ApprovalDecision
from domain.orchestration import (
    OrchestrationCommand,
    OrchestrationCommandType,
    Plan,
    Task,
    TaskExecution,
    TaskStatus,
    WorkerTaskTerminalEvent,
)
from domain.tools import ToolExecutionRequest, ToolExecutionResult
from tests.fakes.leases import acquire_postgres_lease, lease_grant
from infrastructure.persistence.postgres.checkpointer import PostgresCheckpointer
from infrastructure.persistence.postgres.models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    ApprovalRecordModel,
    Base,
    ExecutionLeaseModel,
    OrchestrationCommandModel,
    OrchestrationInboxModel,
    OrchestrationOutboxModel,
    PlanRoundModel,
    PlanVersionModel,
    ProjectModel,
    RootResumeJobModel,
    SessionModel,
    RoundTaskModel,
    TaskExecutionModel,
    WorkerJobModel,
    WorkerToolOperationModel,
)
from infrastructure.persistence.postgres.repositories.approval_repository import (
    PostgresApprovalRepository,
)
from infrastructure.persistence.postgres.repositories.orchestration_command_repository import (
    PostgresOrchestrationCommandRepository,
)
from infrastructure.persistence.postgres.repositories.orchestration_event_repository import (
    PostgresOrchestrationEventRepository,
)
from infrastructure.persistence.postgres.repositories.worker_job_repository import (
    PostgresWorkerJobRepository,
)
from infrastructure.persistence.postgres.repositories.worker_tool_operation_repository import (
    PostgresWorkerToolOperationRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


class _ApprovalToolService:
    """为 checkpoint 验收提供一个需要人工审批的工具。"""

    def __init__(self, executed: list[ToolExecutionRequest] | None = None) -> None:
        """初始化工具执行记录；不接收参数，也不主动抛出业务异常。"""
        self.executed = executed if executed is not None else []

    async def get(self, name: str) -> Any:
        """返回高风险工具配置。

        参数：name 是 Worker 请求查询的工具名。返回启用且需审批的配置。
        不接收未知工具名时抛出异常；验收只会查询 write_file。
        """
        return SimpleNamespace(
            name=name,
            enabled=True,
            risk_level="high",
            require_approval=True,
            execution_mode="native",
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        """记录获批的工具调用并返回确定性成功结果。

        参数：request 是带有原始参数和审批决定的工具请求。返回固定成功结果。
        此测试替身不会主动抛出业务异常。
        """
        self.executed.append(request)
        return ToolExecutionResult(
            status="success",
            output="written",
            duration_ms=1,
        )


class _WorkerAgent:
    """先申请工具审批，恢复后再完成任务的确定性 Worker。"""

    def __init__(self) -> None:
        """将 LLM 调用次数置零；不接收参数，也不主动抛出业务异常。"""
        self.calls = 0
        self.observed_tool_results: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        execution: Mapping[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """首次请求获批工具，第二次调用结束任务。

        参数：execution 是当前 Worker 执行快照；其他关键字参数由工作流提供。
        返回 Worker 图可持久化的 JSON 数据。本替身不主动抛出业务异常。
        """
        self.calls += 1
        tool_results = execution.get("tool_results", [])
        if not tool_results:
            return {
                "status": "running",
                "tool_calls": [
                    {
                        "name": "write_file",
                        "arguments": {
                            "path": "report.txt",
                            "content": "ready",
                        },
                    }
                ],
            }
        self.observed_tool_results = [
            dict(result)
            for result in tool_results
            if isinstance(result, Mapping)
        ]
        return {"status": "completed", "output": {"confirmed": True}}


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected_tool_status", "expected_error"),
    [
        ("approved", "success", None),
        ("denied", "denied", "approval_denied"),
        ("expired", "denied", "approval_expired"),
    ],
)
async def test_worker_approval_resumes_after_postgres_checkpointer_restart(
    decision: str,
    expected_tool_status: str,
    expected_error: str | None,
) -> None:
    """重建 checkpointer 和 executor 后找回 interrupt 并继续同一执行。

    该测试需要可连接且允许创建临时 schema 的 PostgreSQL 测试库；
    测试结束会删除 schema。
    """
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"worker_checkpoint_test_{uuid4().hex}"
    base_url = make_url(DATABASE_URL).set(drivername="postgresql")
    checkpoint_options: dict[str, str | int] = {
        "host": base_url.host or "",
        "port": base_url.port or 5432,
        "dbname": base_url.database or "",
        "user": base_url.username or "",
        "password": base_url.password or "",
        "options": f"-c search_path={schema}",
    }
    checkpoint_options.update(
        {
            name: value
            for name, value in base_url.query.items()
            if isinstance(value, (str, int)) and name != "options"
        }
    )
    checkpoint_connection_string = make_conninfo(**checkpoint_options)
    base_engine = create_async_engine(
        base_url.set(drivername="postgresql+psycopg")
    )
    schema_created = False
    checkpointers: list[PostgresCheckpointer] = []

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        tools = _ApprovalToolService()
        tool_executor = WorkerToolExecutor(tools)
        agent = _WorkerAgent()

        def workflow_factory() -> WorkerWorkflow:
            """创建连接共享测试替身的新工作流。"""
            return WorkerWorkflow(execute=agent, execute_tool=tool_executor)

        execution = TaskExecution(
            plan_id="plan-1",
            run_id="run-1",
            session_id="session-1",
            task=Task(
                "task-1",
                "Write report",
                "Write the requested report",
                allowed_tools=("write_file",),
            ),
            execution_generation=1,
            execution_id="execution-1",
            worker_thread_id="task:task-1:exec:1",
            lease=lease_grant("worker_execution", "execution-1"),
        )

        first_checkpointer = PostgresCheckpointer(
            checkpoint_connection_string
        )
        checkpointers.append(first_checkpointer)
        await first_checkpointer.start()
        first_graph = build_worker_graph(
            workflow=workflow_factory(),
            checkpointer=first_checkpointer.get_saver(),
        )
        paused = await invoke_worker_graph(
            first_graph,
            task_id=execution.task.task_id,
            execution_generation=execution.execution_generation,
            execution=execution,
        )

        interrupts = paused["__interrupt__"]
        assert len(interrupts) == 1
        interrupt_id = str(interrupts[0].id)
        approval = dict(interrupts[0].value)
        assert approval["tool_name"] == "write_file"
        assert tools.executed == []
        assert agent.calls == 1

        await first_checkpointer.stop()
        second_checkpointer = PostgresCheckpointer(
            checkpoint_connection_string
        )
        checkpointers.append(second_checkpointer)
        await second_checkpointer.start()
        recovered_executor = LangGraphWorkerExecutor(
            workflow_factory=workflow_factory,
            checkpointer=second_checkpointer,
        )
        # 模拟新 app 进程：checkpointer 和 Worker agent 都重新创建。
        agent = _WorkerAgent()

        recovered_approval = await recovered_executor.pending_interrupt(execution)

        assert recovered_approval is not None
        assert recovered_approval["interrupt_id"] == interrupt_id
        assert recovered_approval["approval_id"] == approval["approval_id"]
        resumed = await recovered_executor.resume(
            execution,
            {
                **recovered_approval,
                "decision": decision,
            },
        )

        assert resumed.status is TaskStatus.COMPLETED
        assert resumed.output == {"confirmed": True}
        assert len(agent.observed_tool_results) == 1
        tool_result = agent.observed_tool_results[0]
        assert tool_result["status"] == expected_tool_status
        if decision == "approved":
            assert len(tools.executed) == 1
            assert tools.executed[0].arguments == {
                "path": "report.txt",
                "content": "ready",
            }
            assert tools.executed[0].approval_decision == "approved"
        else:
            assert tools.executed == []
            assert tool_result["error"]["code"] == expected_error
        assert agent.calls == 1
    finally:
        for checkpointer in reversed(checkpointers):
            await checkpointer.stop()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approved_worker_effect_resumes_once_after_full_app_restart() -> None:
    """Approval, resume Job, checkpoint, and one tool effect survive restart."""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"worker_approval_restart_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    test_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    checkpoint_options: dict[str, str | int] = {
        "host": base_url.host or "",
        "port": base_url.port or 5432,
        "dbname": base_url.database or "",
        "user": base_url.username or "",
        "password": base_url.password or "",
        "options": f"-c search_path={schema}",
    }
    checkpoint_options.update(
        {
            name: value
            for name, value in base_url.query.items()
            if isinstance(value, (str, int)) and name != "options"
        }
    )
    checkpoint_connection_string = make_conninfo(**checkpoint_options)
    schema_created = False
    now = datetime.now(timezone.utc)
    run_id = f"run-{uuid4().hex}"
    session_id = f"session-{run_id}"
    plan_id = f"plan-{run_id}"
    task_id = f"task-{run_id}"
    checkpointers: list[PostgresCheckpointer] = []
    external_effects: list[ToolExecutionRequest] = []

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        tables = [

            ExecutionLeaseModel.__table__,
            ProjectModel.__table__,
            SessionModel.__table__,
            AgentRunModel.__table__,
            AgentPlanModel.__table__,
            AgentTaskModel.__table__,
            ApprovalRecordModel.__table__,
            OrchestrationCommandModel.__table__,
            OrchestrationInboxModel.__table__,
            OrchestrationOutboxModel.__table__,
            PlanVersionModel.__table__,
            PlanRoundModel.__table__,
            RoundTaskModel.__table__,
            TaskExecutionModel.__table__,
            WorkerJobModel.__table__,
            RootResumeJobModel.__table__,
            WorkerToolOperationModel.__table__,
        ]
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=tables
                )
            )

        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with session_factory() as session, session.begin():
            session.add(ProjectModel(
                id=f"project-{run_id}", name="Worker checkpoint restart",
                created_at=now, updated_at=now,
            ))
            session.add(SessionModel(
                id=session_id, project_id=f"project-{run_id}",
                title="Worker checkpoint restart", active_run_id=run_id,
                last_run_sequence=1, created_at=now,
                updated_at=now, last_activity_at=now,
            ))
            session.add(
                AgentRunModel(
                    run_id=run_id,
                    message_id="message:" + str(run_id),
                    sequence=1,
                    session_id=session_id,
                    status="running",
                    root_thread_id=f"run:{run_id}",
                    created_at=now,
                    updated_at=now,
                )
            )

        command_repository = PostgresOrchestrationCommandRepository(
            session_factory,
            id_factory=lambda: f"command-{uuid4().hex}",
            clock=lambda: now,
        )
        plan = Plan(
            plan_id=plan_id,
            goal="Write the approved report",
            task_ids=(task_id,),
            max_parallelism=1,
            session_id=session_id,
            run_id=run_id,
        )
        root_lease = await acquire_postgres_lease(
            session_factory, "root_execution", run_id
        )
        submitted = await command_repository.submit_plan(
            OrchestrationCommand(
                message_id=f"submit:{run_id}",
                command_type=OrchestrationCommandType.SUBMIT_PLAN,
                idempotency_key=f"submit:{run_id}",
                correlation_id=run_id,
                occurred_at=now,
                lease=root_lease,
                plan_id=plan_id,
                scope_key=run_id,
                plan_version=1,
            ),
            plan,
            [
                Task(
                    task_id=task_id,
                    title="Write report",
                    objective="Write the requested report",
                    allowed_tools=("write_file",),
                )
            ],
        )

        worker_jobs = PostgresWorkerJobRepository(
            session_factory, clock=lambda: now
        )
        first_lease = await worker_jobs.claim_next(
            "worker-before-restart", lease_seconds=60
        )
        assert first_lease is not None and first_lease.job_type == "execute"
        first_execution = TaskExecution(
            plan_id=plan_id,
            run_id=run_id,
            session_id=session_id,
            task=first_lease.task,
            execution_generation=first_lease.attempt,
            execution_id=first_lease.execution_id,
            worker_thread_id=first_lease.worker_thread_id,
            lease=first_lease.lease,
            cancellation_epoch=first_lease.cancellation_epoch,
        )

        first_tool_executor = WorkerToolExecutor(
            _ApprovalToolService(external_effects),
            operation_ledger=PostgresWorkerToolOperationRepository(
                session_factory, clock=lambda: now
            ),
        )
        first_agent = _WorkerAgent()
        first_checkpointer = PostgresCheckpointer(checkpoint_connection_string)
        checkpointers.append(first_checkpointer)
        await first_checkpointer.start()
        first_executor = LangGraphWorkerExecutor(
            workflow_factory=lambda: WorkerWorkflow(
                execute=first_agent,
                execute_tool=first_tool_executor,
            ),
            checkpointer=first_checkpointer,
        )
        paused = await first_executor.execute(first_execution)
        assert paused.status is TaskStatus.WAITING_APPROVAL
        assert paused.approval_batch is not None
        assert not external_effects
        assert await worker_jobs.wait_for_approval(
            first_lease, paused.approval_batch
        )
        approval_id = str(paused.approval_batch["approval_id"])

        approvals = PostgresApprovalRepository(
            session_factory, clock=lambda: now
        )
        assert await approvals.resolve(
            approval_id,
            decision=ApprovalDecision.APPROVED,
        )
        await first_checkpointer.stop()

        # 新进程只从 PostgreSQL 恢复审批决定与 Resume Job，从 checkpoint 恢复 interrupt。
        restarted_checkpointer = PostgresCheckpointer(checkpoint_connection_string)
        checkpointers.append(restarted_checkpointer)
        await restarted_checkpointer.start()
        restarted_jobs = PostgresWorkerJobRepository(
            session_factory, clock=lambda: now
        )
        resumed_lease = await restarted_jobs.claim_next(
            "worker-after-restart", lease_seconds=60
        )
        assert resumed_lease is not None
        assert resumed_lease.job_type == "resume_worker"
        assert resumed_lease.execution_id == first_lease.execution_id
        assert resumed_lease.worker_thread_id == first_lease.worker_thread_id
        assert resumed_lease.resume_value["decision"] == "approved"

        persisted_approval = await approvals.get(approval_id)
        assert persisted_approval is not None
        assert persisted_approval.decision is ApprovalDecision.APPROVED
        resumed_execution = TaskExecution(
            plan_id=plan_id,
            run_id=run_id,
            session_id=session_id,
            task=resumed_lease.task,
            execution_generation=resumed_lease.attempt,
            execution_id=resumed_lease.execution_id,
            worker_thread_id=resumed_lease.worker_thread_id,
            lease=resumed_lease.lease,
            cancellation_epoch=resumed_lease.cancellation_epoch,
        )
        class CrashAfterOperationCommit:
            """模拟账本已提交、Worker checkpoint 尚未前进时进程崩溃。"""

            def __init__(self) -> None:
                self.repository = PostgresWorkerToolOperationRepository(
                    session_factory, clock=lambda: now
                )
                self.crash_once = True

            async def claim(self, request, *, args_hash):
                return await self.repository.claim(request, args_hash=args_hash)

            async def complete(self, request, *, args_hash, result):
                saved = await self.repository.complete(
                    request, args_hash=args_hash, result=result
                )
                if saved and self.crash_once:
                    self.crash_once = False
                    raise asyncio.CancelledError
                return saved

        restarted_tool_executor = WorkerToolExecutor(
            _ApprovalToolService(external_effects),
            operation_ledger=CrashAfterOperationCommit(),
        )
        restarted_agent = _WorkerAgent()
        restarted_executor = LangGraphWorkerExecutor(
            workflow_factory=lambda: WorkerWorkflow(
                execute=restarted_agent,
                execute_tool=restarted_tool_executor,
            ),
            checkpointer=restarted_checkpointer,
        )
        recovered_interrupt = await restarted_executor.pending_interrupt(
            resumed_execution
        )
        assert recovered_interrupt is not None
        assert recovered_interrupt["approval_id"] == approval_id
        assert recovered_interrupt["interrupt_id"] == resumed_lease.resume_value[
            "interrupt_id"
        ]

        with pytest.raises(NodeCancelledError):
            await restarted_executor.resume(
                resumed_execution, dict(resumed_lease.resume_value)
            )
        assert len(external_effects) == 1
        pending_after_crash = await restarted_executor.pending_interrupt(
            resumed_execution
        )
        assert pending_after_crash is not None
        assert pending_after_crash["approval_id"] == approval_id
        async with session_factory() as session:
            committed_operation = await session.get(
                WorkerToolOperationModel,
                external_effects[0].operation_key,
            )
            interrupted_job = await session.get(
                WorkerJobModel, resumed_lease.job_id
            )
        assert committed_operation is not None
        assert committed_operation.status == "completed"
        assert interrupted_job is not None and interrupted_job.status == "running"
        await restarted_checkpointer.stop()

        # Job lease 已过期但 checkpoint 仍在 interrupt；新进程接管并重放账本结果。
        final_checkpointer = PostgresCheckpointer(checkpoint_connection_string)
        checkpointers.append(final_checkpointer)
        await final_checkpointer.start()
        recovery_time = now + timedelta(seconds=61)
        async with session_factory() as session, session.begin():
            await session.execute(
                update(ExecutionLeaseModel)
                .where(
                    ExecutionLeaseModel.resource_type == resumed_lease.lease.key.resource_type,
                    ExecutionLeaseModel.resource_id == resumed_lease.lease.key.resource_id,
                    ExecutionLeaseModel.fencing_token == resumed_lease.lease.fencing_token,
                )
                .values(lease_until=func.clock_timestamp() - text("INTERVAL '1 second'"))
            )
        final_jobs = PostgresWorkerJobRepository(
            session_factory, clock=lambda: recovery_time
        )
        final_lease = await final_jobs.claim_next(
            "worker-after-effect-crash", lease_seconds=60
        )
        assert final_lease is not None
        assert final_lease.job_type == "resume_worker"
        assert final_lease.execution_id == first_lease.execution_id
        assert final_lease.worker_thread_id == first_lease.worker_thread_id
        assert final_lease.lease.fencing_token > resumed_lease.lease.fencing_token
        final_execution = TaskExecution(
            plan_id=plan_id,
            run_id=run_id,
            session_id=session_id,
            task=final_lease.task,
            execution_generation=final_lease.attempt,
            execution_id=final_lease.execution_id,
            worker_thread_id=final_lease.worker_thread_id,
            lease=final_lease.lease,
            cancellation_epoch=final_lease.cancellation_epoch,
        )
        final_agent = _WorkerAgent()
        final_tool_executor = WorkerToolExecutor(
            _ApprovalToolService(external_effects),
            operation_ledger=PostgresWorkerToolOperationRepository(
                session_factory, clock=lambda: recovery_time
            ),
        )
        final_executor = LangGraphWorkerExecutor(
            workflow_factory=lambda: WorkerWorkflow(
                execute=final_agent,
                execute_tool=final_tool_executor,
            ),
            checkpointer=final_checkpointer,
        )
        still_pending = await final_executor.pending_interrupt(final_execution)
        assert still_pending is not None
        assert still_pending["approval_id"] == approval_id
        result = await final_executor.resume(
            final_execution, dict(final_lease.resume_value)
        )
        assert result.status is TaskStatus.COMPLETED
        assert result.output == {"confirmed": True}
        assert final_agent.calls == 1
        assert len(final_agent.observed_tool_results) == 1
        assert len(external_effects) == 1
        assert external_effects[0].approval_decision == "approved"
        assert external_effects[0].arguments == {
            "path": "report.txt",
            "content": "ready",
        }

        receipt = await PostgresOrchestrationEventRepository(
            session_factory, clock=lambda: now
        ).apply_worker_terminal_event(
            WorkerTaskTerminalEvent(
                message_id=f"terminal:{run_id}",
                occurred_at=now,
                worker_id=final_lease.worker_id,
                run_id=run_id,
                plan_id=plan_id,
                plan_version=final_lease.plan_version,
                round_id=submitted.round_id,
                task_id=task_id,
                execution_id=final_lease.execution_id,
                lease=final_lease.lease,
                cancellation_epoch=final_lease.cancellation_epoch,
                status=TaskStatus.COMPLETED,
                result={
                    "output": dict(result.output),
                    "evidence": [],
                    "uncertainties": [],
                    "metrics": {},
                },
            )
        )
        assert receipt.accepted and receipt.round_completed

        async with session_factory() as session:
            approval_row = await session.get(ApprovalRecordModel, approval_id)
            execution_row = await session.get(
                TaskExecutionModel, final_lease.execution_id
            )
            job_rows = (
                await session.execute(
                    select(WorkerJobModel).where(
                        WorkerJobModel.job_type == "resume_worker"
                    )
                )
            ).scalars().all()
            operation_rows = (
                await session.execute(select(WorkerToolOperationModel))
            ).scalars().all()
            operation_count = await session.scalar(
                select(func.count()).select_from(WorkerToolOperationModel)
            )
        assert approval_row is not None
        assert approval_row.status == "resolved"
        assert approval_row.decision == "approved"
        assert execution_row is not None and execution_row.status == "completed"
        assert len(job_rows) == 1 and job_rows[0].status == "completed"
        assert operation_count == 1
        assert len(operation_rows) == 1 and operation_rows[0].status == "completed"
    finally:
        for checkpointer in reversed(checkpointers):
            await checkpointer.stop()
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
