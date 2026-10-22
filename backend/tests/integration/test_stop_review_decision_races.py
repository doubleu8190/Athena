"""PostgreSQL acceptance for Stop racing with Root review decisions."""

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

from domain.orchestration import (
    CommandReceipt,
    OrchestrationCommand,
    OrchestrationCommandType,
    Plan,
    PlanEdge,
    PlanRevision,
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
from infrastructure.persistence.postgres.repositories.run_repository import (
    PostgresRunRepository,
)
from infrastructure.persistence.postgres.repositories.worker_job_repository import (
    PostgresWorkerJobRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("action", [ReviewAction.CONTINUE, ReviewAction.REPLAN])
async def test_stop_racing_continue_or_revise_cancels_all_worker_jobs(
    action: ReviewAction,
) -> None:
    """Stop 与 Continue/Revise 同时提交时不留下可运行的新任务。"""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"stop_review_race_{uuid4().hex}"
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
            goal="Stop safely while Root extends a completed Round",
            task_ids=("task-a", "task-b"),
            edges=(PlanEdge("task-a", "task-b"),),
            max_parallelism=1,
            session_id=session_id,
            run_id=run_id,
        )
        tasks = [
            Task(task_id="task-a", title="A", objective="Complete A"),
            Task(task_id="task-b", title="B", objective="Complete B"),
        ]
        ids = count()
        commands = PostgresOrchestrationCommandRepository(
            session_factory,
            id_factory=lambda: f"generated-{next(ids)}",
            clock=lambda: now,
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
            tasks,
        )
        lease = await PostgresWorkerJobRepository(
            session_factory, clock=lambda: now
        ).claim_next("worker-a", lease_seconds=60)
        assert lease is not None and lease.task_id == "task-a"

        terminal_event = WorkerTaskTerminalEvent(
            message_id=f"terminal:{run_id}",
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
            status=TaskStatus.COMPLETED,
            result={"answer": "A complete"},
        )
        terminal = await PostgresOrchestrationEventRepository(
            session_factory, clock=lambda: now
        ).apply_worker_terminal_event(terminal_event)
        assert terminal.accepted and terminal.round_completed

        revision = (
            PlanRevision(
                base_version=1,
                keep_task_ids=("task-a",),
                cancel_task_ids=("task-b",),
                new_tasks=(
                    {
                        "task_id": "task-c",
                        "title": "C",
                        "objective": "Verify A",
                        "depends_on": ["task-a"],
                    },
                ),
            )
            if action == ReviewAction.REPLAN
            else None
        )
        decision = ReviewDecision(
            action,
            "Continue with the pending task." if revision is None else "Add a verification task.",
            revision=revision,
        )
        command_type = (
            OrchestrationCommandType.REVISE_PLAN
            if action == ReviewAction.REPLAN
            else OrchestrationCommandType.CONTINUE_PLAN
        )
        review_command = OrchestrationCommand(
            message_id=f"review:{run_id}",
            command_type=command_type,
            idempotency_key=f"review:{run_id}",
            correlation_id=run_id,
            occurred_at=now,
            plan_id=plan_id,
            scope_key=run_id,
            plan_version=1,
            round_id=submitted.round_id,
        )
        async def apply_review() -> tuple[CommandReceipt | None, str | None]:
            """在 Stop 竞争下提交 Review，并返回业务拒绝原因。"""
            try:
                receipt = await commands.apply_review_decision(
                    review_command, decision
                )
                return receipt, None
            except PlanCommandRejected as exc:
                return None, str(exc)

        review_result, stop_result = await asyncio.gather(
            apply_review(), PostgresRunRepository(session_factory).cancel(run_id)
        )
        review_receipt, review_rejection = review_result
        assert stop_result is not None and stop_result.status.value == "cancelling"
        assert (review_receipt is None) == (review_rejection is not None)

        async with session_factory() as session:
            run = await session.get(AgentRunModel, run_id)
            plan_row = await session.get(AgentPlanModel, plan_id)
            tasks_after = (
                await session.execute(select(AgentTaskModel))
            ).scalars().all()
            rounds_after = (
                await session.execute(select(PlanRoundModel))
            ).scalars().all()
            jobs_after = (await session.execute(select(WorkerJobModel))).scalars().all()
            resume_jobs = (
                await session.execute(select(RootResumeJobModel))
            ).scalars().all()
            accepted_review_count = await session.scalar(
                select(func.count()).select_from(OrchestrationCommandModel).where(
                    OrchestrationCommandModel.message_id == review_command.message_id
                )
            )
            version_count = await session.scalar(
                select(func.count()).select_from(PlanVersionModel)
            )
        assert run is not None and run.status == "cancelling"
        assert plan_row is not None and plan_row.status == "cancelling"
        assert accepted_review_count == int(review_receipt is not None)
        assert len(rounds_after) == 1 + int(review_receipt is not None)
        assert version_count == 1 + int(
            review_receipt is not None and action == ReviewAction.REPLAN
        )
        assert all(
            task.status not in {"pending", "running", "waiting_approval"}
            for task in tasks_after
        )
        assert all(job.status not in {"pending", "running"} for job in jobs_after)
        assert len(resume_jobs) == 1
        assert resume_jobs[0].status == "cancelled"
    finally:
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
