"""PostgreSQL acceptance for atomic initial Plan and Round persistence."""

from __future__ import annotations

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
    Task,
)
from infrastructure.persistence.postgres.models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    ApprovalRecordModel,
    Base,
    OrchestrationCommandModel,
    OrchestrationOutboxModel,
    PlanRoundModel,
    PlanVersionModel,
    RoundTaskModel,
    TaskExecutionModel,
    WorkerJobModel,
)
from infrastructure.persistence.postgres.repositories.orchestration_command_repository import (
    PostgresOrchestrationCommandRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_submit_plan_persists_version_before_its_round() -> None:
    """PlanVersion FK is satisfied before the initial Round is inserted."""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"plan_submit_{uuid4().hex}"
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
            goal="Answer two independent questions and combine the result",
            task_ids=("task-a", "task-b", "task-merge"),
            edges=(PlanEdge("task-a", "task-merge"), PlanEdge("task-b", "task-merge")),
            max_parallelism=2,
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
            idempotency_key=f"submit-plan:{run_id}",
            correlation_id=run_id,
            occurred_at=now,
            plan_id=plan_id,
            scope_key=run_id,
            plan_version=1,
        )
        ids = count()
        repository = PostgresOrchestrationCommandRepository(
            session_factory, id_factory=lambda: f"generated-{next(ids)}"
        )

        receipt = await repository.submit_plan(command, plan, tasks)

        assert receipt.plan_id == plan_id
        assert receipt.plan_version == 1
        assert receipt.task_ids == ("task-a", "task-b")
        async with session_factory() as session:
            assert await session.scalar(
                select(func.count()).select_from(PlanVersionModel)
            ) == 1
            assert await session.scalar(
                select(func.count()).select_from(PlanRoundModel)
            ) == 1
            assert await session.scalar(
                select(func.count()).select_from(RoundTaskModel)
            ) == 2
            assert await session.scalar(
                select(func.count()).select_from(TaskExecutionModel)
            ) == 2
            assert await session.scalar(
                select(func.count()).select_from(WorkerJobModel)
            ) == 2
            persisted_run = await session.get(AgentRunModel, run_id)
            assert persisted_run is not None
            assert persisted_run.status == "running"
    finally:
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await test_engine.dispose()
        await base_engine.dispose()
