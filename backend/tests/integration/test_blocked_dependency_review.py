"""PostgreSQL acceptance for dependency failures at the Root Review boundary."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from itertools import count
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from domain.orchestration import (
    OrchestrationCommand,
    OrchestrationCommandType,
    Plan,
    PlanEdge,
    PlanStatus,
    ReviewAction,
    ReviewDecision,
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
    RootResumeJobModel,
    RoundTaskModel,
    TaskExecutionModel,
    WorkerJobModel,
)
from infrastructure.persistence.postgres.repositories.orchestration_command_repository import (
    PlanCommandRejected,
    PostgresOrchestrationCommandRepository,
)
from infrastructure.persistence.postgres.repositories.orchestration_event_repository import (
    PostgresOrchestrationEventRepository,
)
from infrastructure.persistence.postgres.repositories.orchestration_repository import (
    PostgresOrchestrationRepository,
)
from infrastructure.persistence.postgres.repositories.worker_job_repository import (
    PostgresWorkerJobRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_blocked_continue_returns_explicit_error_then_finish_cancels_pending_tasks() -> None:
    """失败依赖不生成空 Round，有限 Review 最终可安全终结 Plan。"""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"blocked_review_{uuid4().hex}"
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

        ids = count()
        commands = PostgresOrchestrationCommandRepository(
            session_factory,
            id_factory=lambda: f"generated-{next(ids)}",
            clock=lambda: now,
        )
        plan = Plan(
            plan_id=plan_id,
            goal="Complete B only after prerequisite A succeeds",
            task_ids=("task-a", "task-b"),
            edges=(PlanEdge("task-a", "task-b"),),
            max_parallelism=1,
            session_id=session_id,
            run_id=run_id,
        )
        submitted = await commands.submit_plan(
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
            plan,
            [
                Task(task_id="task-a", title="A", objective="Complete A"),
                Task(task_id="task-b", title="B", objective="Complete B"),
            ],
        )
        lease = await PostgresWorkerJobRepository(
            session_factory, clock=lambda: now
        ).claim_next("worker-a", lease_seconds=60)
        assert lease is not None and lease.task_id == "task-a"

        failure = WorkerTaskTerminalEvent(
            message_id=f"failed:{run_id}",
            occurred_at=now,
            worker_id=lease.worker_id,
            run_id=run_id,
            plan_id=plan_id,
            plan_version=lease.plan_version,
            round_id=submitted.round_id,
            task_id="task-a",
            execution_id=lease.execution_id,
            fencing_token=lease.fencing_token,
            cancellation_epoch=lease.cancellation_epoch,
            status=TaskStatus.FAILED,
            result={"error": "prerequisite unavailable"},
            error={"code": "prerequisite_unavailable"},
            retryable=False,
        )
        failed = await PostgresOrchestrationEventRepository(
            session_factory, clock=lambda: now
        ).apply_worker_terminal_event(failure)
        assert failed.accepted and failed.round_completed

        repository = PostgresOrchestrationRepository(session_factory)
        review_context = await repository.load_review_context(submitted.round_id)
        assert review_context.remaining_tasks == (
            {
                "task_id": "task-b",
                "title": "B",
                "instruction": "Complete B",
                "depends_on": ["task-a"],
                "status": "pending",
                "blocked_by": ["task-a"],
            },
        )

        identity = {
            "message_id": f"review:{run_id}",
            "idempotency_key": f"root-review:{submitted.round_id}",
            "correlation_id": run_id,
            "occurred_at": now,
            "plan_id": plan_id,
            "scope_key": run_id,
            "plan_version": 1,
            "round_id": submitted.round_id,
        }
        with pytest.raises(PlanCommandRejected) as rejection:
            await commands.apply_review_decision(
                OrchestrationCommand(
                    command_type=OrchestrationCommandType.CONTINUE_PLAN,
                    **identity,
                ),
                ReviewDecision(ReviewAction.CONTINUE, "Continue."),
            )
        assert rejection.value.code == "BLOCKED_DEPENDENCIES"

        async with session_factory() as session:
            plan_row = await session.get(AgentPlanModel, plan_id)
            task_b = await session.get(AgentTaskModel, "task-b")
            round_count = await session.scalar(
                select(func.count()).select_from(PlanRoundModel)
            )
            worker_job_count = await session.scalar(
                select(func.count()).select_from(WorkerJobModel)
            )
            review_command_count = await session.scalar(
                select(func.count()).select_from(OrchestrationCommandModel).where(
                    OrchestrationCommandModel.idempotency_key
                    == identity["idempotency_key"]
                )
            )
        assert plan_row is not None
        assert plan_row.status == PlanStatus.WAITING_REVIEW.value
        assert task_b is not None and task_b.status == TaskStatus.PENDING.value
        assert round_count == 1
        assert worker_job_count == 1
        assert review_command_count == 0

        finished = await commands.apply_review_decision(
            OrchestrationCommand(
                command_type=OrchestrationCommandType.FINISH_PLAN,
                **identity,
            ),
            ReviewDecision(
                ReviewAction.FINISH,
                "No runnable recovery task remains.",
                final_status="failed",
            ),
        )
        assert finished.task_ids == ()
        async with session_factory() as session:
            plan_row = await session.get(AgentPlanModel, plan_id)
            task_b = await session.get(AgentTaskModel, "task-b")
            plan_failed_count = await session.scalar(
                select(func.count()).select_from(OrchestrationOutboxModel).where(
                    OrchestrationOutboxModel.event_type == "PlanFailed"
                )
            )
            round_count = await session.scalar(
                select(func.count()).select_from(PlanRoundModel)
            )
            worker_jobs = (
                await session.execute(select(WorkerJobModel))
            ).scalars().all()
        assert plan_row is not None and plan_row.status == PlanStatus.FAILED.value
        assert task_b is not None and task_b.status == TaskStatus.CANCELLED.value
        assert json.loads(task_b.error_json) == {"reason": "plan_finished"}
        assert plan_failed_count == 1
        assert round_count == 1
        assert len(worker_jobs) == 1 and worker_jobs[0].status == "completed"
    finally:
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
