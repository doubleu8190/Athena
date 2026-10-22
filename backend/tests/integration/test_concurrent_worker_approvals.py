"""PostgreSQL acceptance for independent Worker approval interrupts."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from itertools import count
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from domain.approval import ApprovalDecision
from domain.orchestration import (
    OrchestrationCommand,
    OrchestrationCommandType,
    Plan,
    Task,
    TaskStatus,
    WorkerTaskTerminalEvent,
)
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
    RoundTaskModel,
    RootResumeJobModel,
    TaskExecutionModel,
    WorkerJobModel,
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
from infrastructure.persistence.postgres.repositories.run_repository import (
    PostgresRunRepository,
)
from infrastructure.persistence.postgres.repositories.worker_job_repository import (
    PostgresWorkerJobRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolving_one_worker_approval_only_unblocks_its_execution() -> None:
    """Independent approvals create and release only their own resume jobs."""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"worker_approvals_{uuid4().hex}"
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
            OrchestrationOutboxModel.__table__,
            PlanVersionModel.__table__,
            PlanRoundModel.__table__,
            RoundTaskModel.__table__,
            TaskExecutionModel.__table__,
            WorkerJobModel.__table__,
            RootResumeJobModel.__table__,
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
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )

        plan = Plan(
            plan_id=plan_id,
            goal="Resolve two independent Worker tool approvals",
            task_ids=("task-a", "task-b", "task-c"),
            max_parallelism=3,
            session_id=session_id,
            run_id=run_id,
        )
        tasks = [
            Task(task_id=task_id, title=task_id, objective=f"Complete {task_id}")
            for task_id in plan.task_ids
        ]
        command = OrchestrationCommand(
            message_id=f"submit:{run_id}",
            command_type=OrchestrationCommandType.SUBMIT_PLAN,
            idempotency_key=f"submit:{run_id}",
            correlation_id=run_id,
            occurred_at=now,
            plan_id=plan_id,
            scope_key=run_id,
            plan_version=1,
        )
        ids = count()
        command_repository = PostgresOrchestrationCommandRepository(
            session_factory,
            id_factory=lambda: f"generated-{next(ids)}",
            clock=lambda: now,
        )
        receipt = await command_repository.submit_plan(command, plan, tasks)
        assert set(receipt.task_ids) == {"task-a", "task-b", "task-c"}

        worker_jobs = PostgresWorkerJobRepository(
            session_factory, clock=lambda: now
        )
        leases = [
            await worker_jobs.claim_next(worker_id, lease_seconds=60)
            for worker_id in ("worker-a", "worker-b", "worker-c")
        ]
        assert all(lease is not None for lease in leases)
        leases_by_task = {
            lease.task_id: lease for lease in leases if lease is not None
        }
        assert set(leases_by_task) == {"task-a", "task-b", "task-c"}

        approvals = {}
        for task_id, lease in leases_by_task.items():
            suffix = task_id[-1]
            approval_id = f"approval-{suffix}-{run_id}"
            approval = {
                "approval_id": approval_id,
                "run_id": run_id,
                "plan_id": plan_id,
                "task_id": task_id,
                "execution_id": lease.execution_id,
                "worker_thread_id": lease.worker_thread_id,
                "interrupt_id": f"interrupt-{suffix}",
                "tool_call_id": f"call-{suffix}",
                "tool_name": "write_file",
                "arguments": {"path": f"{suffix}.txt", "content": suffix},
                "risk_level": "high",
                "args_hash": f"hash-{suffix}",
                "operation_key": f"operation-{suffix}",
            }
            assert await worker_jobs.wait_for_approval(lease, approval)
            approvals[task_id] = approval_id

        approval_repository = PostgresApprovalRepository(
            session_factory, clock=lambda: now
        )
        assert await approval_repository.resolve(
            approvals["task-a"],
            run_id=run_id,
            task_id="task-a",
            decision=ApprovalDecision.APPROVED,
        )
        resumed_a = await worker_jobs.claim_next("resume-a", lease_seconds=60)
        assert resumed_a is not None and resumed_a.task_id == "task-a"
        assert resumed_a.job_type == "resume_worker"
        assert await worker_jobs.claim_next("resume-b-too-early", lease_seconds=60) is None

        async with session_factory() as session:
            approval_a = await session.get(
                ApprovalRecordModel, approvals["task-a"]
            )
            approval_b = await session.get(
                ApprovalRecordModel, approvals["task-b"]
            )
            resume_rows = (
                await session.execute(
                    select(WorkerJobModel).where(
                        WorkerJobModel.job_type == "resume_worker"
                    )
                )
            ).scalars().all()
        assert approval_a is not None and approval_a.decision == "approved"
        assert approval_b is not None and approval_b.status == "pending"
        assert len(resume_rows) == 1
        assert resume_rows[0].approval_id == approvals["task-a"]

        assert await approval_repository.resolve(
            approvals["task-b"],
            run_id=run_id,
            task_id="task-b",
            decision=ApprovalDecision.DENIED,
        )
        resumed_b = await worker_jobs.claim_next("resume-b", lease_seconds=60)
        assert resumed_b is not None and resumed_b.task_id == "task-b"
        assert resumed_b.job_type == "resume_worker"

        approval_result, cancellation_result = await asyncio.gather(
            approval_repository.resolve(
                approvals["task-c"],
                run_id=run_id,
                task_id="task-c",
                decision=ApprovalDecision.APPROVED,
            ),
            PostgresRunRepository(session_factory).cancel(run_id),
        )
        assert approval_result in {True, False}
        assert cancellation_result is not None
        assert cancellation_result.status.value == "cancelling"

        async with session_factory() as session:
            run = await session.get(AgentRunModel, run_id)
            plan_row = await session.get(AgentPlanModel, plan_id)
            task_c = await session.get(AgentTaskModel, "task-c")
            approval_c = await session.get(
                ApprovalRecordModel, approvals["task-c"]
            )
            approval_b = await session.get(
                ApprovalRecordModel, approvals["task-b"]
            )
            resume_rows = (
                await session.execute(
                    select(WorkerJobModel).where(
                        WorkerJobModel.job_type == "resume_worker"
                    )
                )
            ).scalars().all()
            root_resume_count = await session.scalar(
                select(func.count()).select_from(RootResumeJobModel)
            )
            pending_approval_count = await session.scalar(
                select(func.count()).select_from(ApprovalRecordModel).where(
                    ApprovalRecordModel.status == "pending"
                )
            )
        assert run is not None and run.status == "cancelling"
        assert plan_row is not None and plan_row.status == "cancelling"
        assert task_c is not None and task_c.status == "cancelled"
        assert approval_c is not None
        assert approval_c.status in {"resolved", "cancelled"}
        if approval_result:
            assert approval_c.status == "resolved"
            assert approval_c.decision == "approved"
        else:
            assert approval_c.status == "cancelled"
        assert approval_b is not None and approval_b.decision == "denied"
        assert {row.approval_id for row in resume_rows} == set(approvals.values())
        assert pending_approval_count == 0
        assert all(
            row.status not in {"pending", "running"}
            for row in resume_rows
            if row.approval_id == approvals["task-c"]
        )
        assert root_resume_count == 0
    finally:
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stop_racing_last_worker_terminal_event_leaves_no_root_resume() -> None:
    """Stop and the final Worker event linearize on the Run row in PostgreSQL."""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"stop_terminal_race_{uuid4().hex}"
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
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )

        plan = Plan(
            plan_id=plan_id,
            goal="Complete one Worker task while Stop may arrive",
            task_ids=("task-only",),
            max_parallelism=1,
            session_id=session_id,
            run_id=run_id,
        )
        command = OrchestrationCommand(
            message_id=f"submit:{run_id}",
            command_type=OrchestrationCommandType.SUBMIT_PLAN,
            idempotency_key=f"submit:{run_id}",
            correlation_id=run_id,
            occurred_at=now,
            plan_id=plan_id,
            scope_key=run_id,
            plan_version=1,
        )
        ids = count()
        command_repository = PostgresOrchestrationCommandRepository(
            session_factory,
            id_factory=lambda: f"generated-{next(ids)}",
            clock=lambda: now,
        )
        submitted = await command_repository.submit_plan(
            command,
            plan,
            [Task(task_id="task-only", title="Task", objective="Finish")],
        )
        lease = await PostgresWorkerJobRepository(
            session_factory, clock=lambda: now
        ).claim_next("worker", lease_seconds=60)
        assert lease is not None

        terminal_event = WorkerTaskTerminalEvent(
            message_id=f"terminal:{run_id}",
            occurred_at=now,
            worker_id=lease.worker_id,
            run_id=run_id,
            plan_id=plan_id,
            plan_version=lease.plan_version,
            round_id=submitted.round_id,
            task_id="task-only",
            execution_id=lease.execution_id,
            fencing_token=lease.fencing_token,
            cancellation_epoch=lease.cancellation_epoch,
            status=TaskStatus.COMPLETED,
            result={"answer": "completed"},
        )
        event_repository = PostgresOrchestrationEventRepository(
            session_factory,
            clock=lambda: now,
            id_factory=lambda: f"event-{uuid4().hex}",
        )
        event_receipt, stop_receipt = await asyncio.gather(
            event_repository.apply_worker_terminal_event(terminal_event),
            PostgresRunRepository(session_factory).cancel(run_id),
        )

        assert stop_receipt is not None
        assert stop_receipt.status.value == "cancelling"
        async with session_factory() as session:
            run = await session.get(AgentRunModel, run_id)
            plan_row = await session.get(AgentPlanModel, plan_id)
            task = await session.get(AgentTaskModel, "task-only")
            round_row = await session.get(PlanRoundModel, submitted.round_id)
            resume_jobs = (
                await session.execute(select(RootResumeJobModel))
            ).scalars().all()
            inbox = (
                await session.execute(select(OrchestrationInboxModel))
            ).scalars().all()
            outbox = (
                await session.execute(select(OrchestrationOutboxModel))
            ).scalars().all()
        assert run is not None and run.status == "cancelling"
        assert plan_row is not None and plan_row.status == "cancelling"
        assert task is not None
        assert event_receipt.accepted is (task.status == "completed")
        assert event_receipt.accepted is (
            round_row is not None and round_row.status == "completed"
        )
        assert len(inbox) == 1 and inbox[0].status == "processed"
        assert len(resume_jobs) == int(event_receipt.accepted)
        assert all(job.status == "cancelled" for job in resume_jobs)
        assert sum(row.event_type == "RoundCompleted" for row in outbox) == int(
            event_receipt.accepted
        )
        assert sum(row.event_type == "RunCancelling" for row in outbox) == 1
    finally:
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
