"""PostgreSQL acceptance for expired Worker lease recovery and fencing."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from infrastructure.persistence.postgres.models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    ApprovalRecordModel,
    Base,
    OrchestrationOutboxModel,
    PlanRoundModel,
    PlanVersionModel,
    RoundTaskModel,
    TaskExecutionModel,
    WorkerJobModel,
)
from infrastructure.persistence.postgres.repositories.worker_job_repository import (
    PostgresWorkerJobRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_worker_lease_is_reclaimed_once_with_new_fence() -> None:
    """并发接管过期租约时只授予一个 Worker，并隔离旧持有者。

    该测试需要可连接且允许创建临时 schema 的 PostgreSQL 测试库；
    测试结束会删除 schema。
    """
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"worker_lease_test_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    schema_url = base_url.update_query_dict(
        {"options": f"-c search_path={schema}"}
    )
    test_engine = create_async_engine(schema_url)
    schema_created = False
    now = datetime.now(timezone.utc)

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        required_tables = [
            AgentRunModel.__table__,
            AgentPlanModel.__table__,
            AgentTaskModel.__table__,
            ApprovalRecordModel.__table__,
            PlanVersionModel.__table__,
            PlanRoundModel.__table__,
            RoundTaskModel.__table__,
            TaskExecutionModel.__table__,
            WorkerJobModel.__table__,
            OrchestrationOutboxModel.__table__,
        ]
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=required_tables
                )
            )

        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        await _seed_expired_worker_job(session_factory, now=now)
        repository = PostgresWorkerJobRepository(
            session_factory,
            clock=lambda: now,
        )

        first_claim, second_claim = await asyncio.gather(
            repository.claim_next("worker-a", lease_seconds=60),
            repository.claim_next("worker-b", lease_seconds=60),
        )

        claims = [claim for claim in (first_claim, second_claim) if claim is not None]
        assert len(claims) == 1
        current_lease = claims[0]
        assert current_lease.fencing_token == 8
        assert current_lease.attempt == 1
        assert current_lease.worker_id in {"worker-a", "worker-b"}

        previous_lease = replace(
            current_lease,
            worker_id="crashed-worker",
            fencing_token=7,
            lease_until=now - timedelta(seconds=1),
        )
        assert (
            await repository.renew_lease(previous_lease, lease_seconds=60)
        ) is None

        async with session_factory() as session:
            execution = await session.get(TaskExecutionModel, "execution-1")
            job = await session.get(WorkerJobModel, "job-1")

        assert execution is not None
        assert job is not None
        assert execution.lease_owner == current_lease.worker_id
        assert execution.fencing_token == 8
        assert execution.attempt == 1
        assert job.lease_owner == current_lease.worker_id
        assert job.attempt == 2
    finally:
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()


async def _seed_expired_worker_job(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> None:
    """创建一条崩溃 Worker 留下的执行链和已过期租约。

    参数：session_factory 是隔离 schema 的 async_sessionmaker[AsyncSession]；
    now 是测试使用的 UTC datetime。返回值为 None；数据库写入失败时抛出异常。
    """
    async with session_factory() as session, session.begin():
        session.add(
            AgentRunModel(
                run_id="run-1",
                session_id="session-1",
                status="running",
                root_thread_id="run:run-1",
                cancellation_epoch=0,
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
            )
        )
        await session.flush()
        session.add(
            AgentPlanModel(
                plan_id="plan-1",
                run_id="run-1",
                session_id="session-1",
                status="running",
                user_goal="recover an interrupted Worker",
                max_parallelism=1,
                plan_json="{}",
                current_version=1,
                active_round_id="round-1",
                cancellation_epoch=0,
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
            )
        )
        await session.flush()
        session.add(
            AgentTaskModel(
                task_id="task-1",
                plan_id="plan-1",
                run_id="run-1",
                title="Recover task",
                objective="Resume after Worker process loss",
                allowed_tools_json="[]",
                timeout_seconds=600,
                status="running",
                execution_generation=1,
                active_execution_id="execution-1",
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
            )
        )
        session.add(
            PlanVersionModel(
                plan_id="plan-1",
                version=1,
                dag_json={"edges": []},
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            PlanRoundModel(
                round_id="round-1",
                plan_id="plan-1",
                plan_version=1,
                round_number=1,
                status="running",
                expected_task_count=1,
                terminal_task_count=0,
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            RoundTaskModel(
                round_id="round-1",
                task_id="task-1",
                status="running",
                active_execution_id="execution-1",
            )
        )
        await session.flush()
        expired_at = now - timedelta(seconds=10)
        session.add(
            TaskExecutionModel(
                execution_id="execution-1",
                round_id="round-1",
                task_id="task-1",
                attempt=1,
                worker_thread_id="task:task-1:exec:1",
                status="running",
                lease_owner="crashed-worker",
                lease_until=expired_at,
                fencing_token=7,
                cancellation_epoch=0,
                created_at=now - timedelta(minutes=1),
                updated_at=expired_at,
            )
        )
        await session.flush()
        session.add(
            WorkerJobModel(
                job_id="job-1",
                dedupe_key="execution:execution-1",
                job_type="execute",
                execution_id="execution-1",
                payload_json={},
                status="running",
                attempt=1,
                available_at=now - timedelta(minutes=1),
                lease_owner="crashed-worker",
                lease_until=expired_at,
                created_at=now - timedelta(minutes=1),
                updated_at=expired_at,
            )
        )
