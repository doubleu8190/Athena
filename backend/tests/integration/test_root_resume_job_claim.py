"""PostgreSQL acceptance for competing Root Resume dispatchers."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from infrastructure.persistence.postgres.models import (
    AgentPlanModel,
    AgentRunModel,
    Base,
    ExecutionLeaseModel,
    PlanRoundModel,
    PlanVersionModel,
    ProjectModel,
    RootResumeJobModel,
    SessionModel,
)
from infrastructure.persistence.postgres.repositories.root_resume_job_repository import (
    PostgresRootResumeJobRepository,
)
from infrastructure.persistence.postgres.repositories.run_repository import (
    PostgresRunRepository,
)
from tests.fakes.leases import LeaseCenter, lease_grant
from workers.root_resume_dispatcher import RootResumeDispatcher


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_app_instances_claim_one_root_resume_and_wait_for_interrupt() -> None:
    """A single lease waits durably until the Root checkpoint is armed."""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"root_resume_claim_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    app_a_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    app_b_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    schema_created = False
    now = datetime.now(timezone.utc)
    run_id = f"run-{uuid4().hex}"
    plan_id = f"plan-{run_id}"
    round_id = f"round-{run_id}"
    job_id = f"resume-{run_id}"

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
            PlanVersionModel.__table__,
            PlanRoundModel.__table__,
            RootResumeJobModel.__table__,
        ]
        async with app_a_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=tables
                )
            )

        app_a_sessions = async_sessionmaker(app_a_engine, expire_on_commit=False)
        app_b_sessions = async_sessionmaker(app_b_engine, expire_on_commit=False)
        async with app_a_sessions() as session, session.begin():
            session_id = f"session-{run_id}"
            project_id = f"project-{run_id}"
            session.add(ProjectModel(
                id=project_id, name="Root resume claim",
                created_at=now.isoformat(), updated_at=now.isoformat(),
            ))
            session.add(SessionModel(
                id=session_id, project_id=project_id, title="Root resume claim",
                active_run_id=run_id, last_run_sequence=1,
                created_at=now.isoformat(), updated_at=now.isoformat(),
                last_activity_at=now.isoformat(),
            ))
            session.add(
                AgentRunModel(
                    run_id=run_id,
                    message_id="message:" + str(run_id),
                    sequence=1,
                    session_id=session_id,
                    status="running",
                    root_thread_id=f"run:{run_id}",
                    root_wait_generation=4,
                    root_wait_checkpoint_id=None,
                    root_wait_armed_at=None,
                    root_wait_round_id=round_id,
                    root_wait_plan_version=1,
                    root_wait_interrupt_id=None,
                    cancellation_epoch=0,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            session.add(
                AgentPlanModel(
                    plan_id=plan_id,
                    run_id=run_id,
                session_id=session_id,
                    status="waiting_review",
                    user_goal="Complete one round and resume Root exactly once",
                    max_parallelism=1,
                    current_version=1,
                    active_round_id=round_id,
                    cancellation_epoch=0,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            await session.flush()
            session.add(
                PlanVersionModel(
                    plan_id=plan_id,
                    version=1,
                    dag_json={"nodes": [], "edges": []},
                    created_at=now,
                )
            )
            await session.flush()
            session.add(
                PlanRoundModel(
                    round_id=round_id,
                    plan_id=plan_id,
                    plan_version=1,
                    round_number=1,
                    status="completed",
                    expected_task_count=1,
                    terminal_task_count=1,
                    created_at=now,
                    completed_at=now,
                )
            )
            await session.flush()
            session.add(
                RootResumeJobModel(
                    job_id=job_id,
                    dedupe_key=f"resume:{run_id}:4",
                    run_id=run_id,
                    plan_id=plan_id,
                    round_id=round_id,
                    root_wait_generation=4,
                    status="pending",
                    attempt=0,
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )

        repo_a = PostgresRootResumeJobRepository(app_a_sessions, clock=lambda: now)
        repo_b = PostgresRootResumeJobRepository(app_b_sessions, clock=lambda: now)
        claims = await asyncio.gather(
            repo_a.claim_next("app-a-root-resumer", lease_seconds=30),
            repo_b.claim_next("app-b-root-resumer", lease_seconds=30),
        )

        leases = [claim for claim in claims if claim is not None]
        assert len(leases) == 1
        lease = leases[0]
        assert lease.job_id == job_id
        assert lease.root_wait_generation == 4
        assert lease.worker_id in {"app-a-root-resumer", "app-b-root-resumer"}
        assert lease.interrupt_id is None
        assert lease.checkpoint_id is None

        owner_repository = (
            repo_a if lease.worker_id == "app-a-root-resumer" else repo_b
        )
        assert await owner_repository.retry(lease, delay_seconds=0)
        run_repository = PostgresRunRepository(app_a_sessions)
        root_grant = await run_repository.acquire_execution_lease(
            run_id, owner="root-wait-arm", lease_seconds=30
        )
        assert root_grant is not None
        assert await run_repository.arm_root_wait(
            run_id,
            plan_id=plan_id,
            round_id=round_id,
            plan_version=1,
            interrupt_id="interrupt-4",
            checkpoint_id="checkpoint-4",
            lease=root_grant,
        )
        armed_lease = await owner_repository.claim_next(
            lease.worker_id, lease_seconds=30
        )
        assert armed_lease is not None
        assert armed_lease.job_id == job_id
        assert armed_lease.interrupt_id == "interrupt-4"
        assert armed_lease.checkpoint_id == "checkpoint-4"

        class Graph:
            async def has_pending_round_wait(self, **identity):
                return identity == {
                    "run_id": run_id,
                    "plan_id": plan_id,
                    "round_id": round_id,
                    "plan_version": 1,
                    "interrupt_id": "interrupt-4",
                    "checkpoint_id": "checkpoint-4",
                }

        class Execution:
            def __init__(self) -> None:
                self.commands = []

            async def execute(self, command):
                self.commands.append(command)
                return {"content": "reviewed"}

        class Runs:
            async def get(self, _run_id):
                return SimpleNamespace(status="completed")

            async def acquire_execution_lease(
                self, _run_id, *, owner, lease_seconds
            ):
                return lease_grant("root_execution", _run_id, owner_id=owner)

        execution = Execution()
        dispatcher = RootResumeDispatcher(
            owner_repository,
            Graph(),
            execution,
            Runs(),
            worker_id=lease.worker_id,
            lease_center=LeaseCenter(),
        )
        await dispatcher._resume(armed_lease)
        assert len(execution.commands) == 1
        assert (
            execution.commands[0].payload.resume_value.interrupt_id
            == "interrupt-4"
        )

        async with app_a_sessions() as session:
            job = await session.get(RootResumeJobModel, job_id)
            execution_lease = await session.get(
                ExecutionLeaseModel, ("root_resume", job_id)
            )
        assert job is not None
        assert job.status == "completed"
        assert execution_lease is not None
        assert execution_lease.state == "released"
        assert execution_lease.owner_id is None
        assert execution_lease.lease_until is None
        assert job.attempt == 2
    finally:
        await app_b_engine.dispose()
        await app_a_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
