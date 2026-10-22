"""PostgreSQL acceptance for external effects racing Run cancellation."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from application.orchestration.worker_tools import WorkerToolExecutor
from domain.orchestration import (
    OrchestrationCommand,
    OrchestrationCommandType,
    Plan,
    Task,
)
from domain.tools import ToolExecutionRequest
from infrastructure.persistence.postgres.models import (
    AgentCommandModel,
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    ApprovalRecordModel,
    Base,
    OrchestrationCommandModel,
    OrchestrationInboxModel,
    OrchestrationOutboxModel,
    PlanRoundModel,
    PlanVersionModel,
    RootResumeJobModel,
    RoundTaskModel,
    TaskExecutionModel,
    WorkerJobModel,
    WorkerToolOperationModel,
)
from infrastructure.persistence.postgres.repositories.orchestration_command_repository import (
    PostgresOrchestrationCommandRepository,
)
from infrastructure.persistence.postgres.repositories.run_repository import (
    PostgresRunRepository,
)
from infrastructure.persistence.postgres.repositories.worker_job_repository import (
    PostgresWorkerJobRepository,
)
from infrastructure.persistence.postgres.repositories.worker_tool_operation_repository import (
    PostgresWorkerToolOperationRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_external_effect_survives_cancel_as_unknown_and_is_audited() -> None:
    """A sent API request remains auditable and is never blindly replayed."""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"external_effect_cancel_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    test_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    schema_created = False
    now = datetime.now(timezone.utc)
    run_id = f"run-{uuid4().hex}"
    session_id = f"session-{run_id}"
    plan_id = f"plan-{run_id}"
    task_id = f"task-{run_id}"

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True
        tables = [
            AgentCommandModel.__table__,
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
            session.add(
                AgentRunModel(
                    run_id=run_id,
                    session_id=session_id,
                    status="created",
                    root_thread_id=f"run:{run_id}",
                    cancellation_epoch=0,
                    cancellation_external_effect_status="none",
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )

        submitted = await PostgresOrchestrationCommandRepository(
            session_factory,
            id_factory=lambda: f"command-{uuid4().hex}",
            clock=lambda: now,
        ).submit_plan(
            OrchestrationCommand(
                message_id=f"submit:{run_id}",
                command_type=OrchestrationCommandType.SUBMIT_PLAN,
                idempotency_key=f"submit:{run_id}",
                correlation_id=run_id,
                occurred_at=now,
                plan_id=plan_id,
                scope_key=run_id,
                plan_version=1,
            ),
            Plan(
                plan_id=plan_id,
                goal="Send a non-cancellable external request",
                task_ids=(task_id,),
                max_parallelism=1,
                session_id=session_id,
                run_id=run_id,
            ),
            [
                Task(
                    task_id=task_id,
                    title="Send request",
                    objective="Issue one external request",
                    allowed_tools=("send_external_request",),
                )
            ],
        )
        worker_jobs = PostgresWorkerJobRepository(
            session_factory, clock=lambda: now
        )
        lease = await worker_jobs.claim_next("worker-before-stop", lease_seconds=60)
        assert lease is not None

        class NonCancellableExternalAPI:
            def __init__(self) -> None:
                self.sent: list[ToolExecutionRequest] = []

            async def get(self, name: str):
                return SimpleNamespace(
                    name=name,
                    enabled=True,
                    risk_level="low",
                    require_approval=False,
                    execution_mode="native",
                )

            async def execute(self, request: ToolExecutionRequest):
                self.sent.append(request)
                # 模拟服务端已经接收请求，但客户端在得到回执前失联。
                raise asyncio.CancelledError

        external_api = NonCancellableExternalAPI()
        operation_ledger = PostgresWorkerToolOperationRepository(
            session_factory, clock=lambda: now
        )
        tool_executor = WorkerToolExecutor(
            external_api,
            operation_ledger=operation_ledger,
        )
        execution = {
            "run_id": lease.run_id,
            "plan_id": lease.plan_id,
            "session_id": lease.session_id,
            "execution_id": lease.execution_id,
            "worker_thread_id": lease.worker_thread_id,
            "execution_generation": lease.attempt,
            "fencing_token": lease.fencing_token,
            "cancellation_epoch": lease.cancellation_epoch,
            "task": {
                "task_id": lease.task_id,
                "allowed_tools": list(lease.task.allowed_tools),
            },
        }
        with pytest.raises(asyncio.CancelledError):
            await tool_executor(
                tool_call={
                    "name": "send_external_request",
                    "arguments": {"record_id": "remote-123", "action": "create"},
                },
                execution=execution,
                tool_round=0,
                call_index=0,
            )
        assert len(external_api.sent) == 1

        cancellation = await PostgresRunRepository(session_factory).cancel(run_id)
        assert cancellation is not None
        assert cancellation.status.value == "cancelling"
        assert cancellation.external_effect_status == "unknown"
        await PostgresWorkerJobRepository(
            session_factory, clock=lambda: now + timedelta(seconds=61)
        ).reconcile_cancellations()

        async with session_factory() as session:
            run = await session.get(AgentRunModel, run_id)
            operations = (
                await session.execute(select(WorkerToolOperationModel))
            ).scalars().all()
            operation_count = await session.scalar(
                select(func.count()).select_from(WorkerToolOperationModel)
            )
        assert run is not None and run.status == "cancelled"
        assert run.cancellation_external_effect_status == "unknown"
        assert operation_count == 1
        assert operations[0].status == "executing"
        assert operations[0].tool_call_id == external_api.sent[0].tool_call_id
        assert operations[0].args_hash

        retry_result = await tool_executor(
            tool_call={
                "name": "send_external_request",
                "arguments": {"record_id": "remote-123", "action": "create"},
            },
            execution=execution,
            tool_round=0,
            call_index=0,
        )
        assert retry_result["status"] == "denied"
        assert retry_result["error"]["code"] == "worker_operation_lease_not_active"
        assert len(external_api.sent) == 1
    finally:
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
