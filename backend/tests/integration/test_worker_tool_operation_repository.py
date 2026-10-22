"""PostgreSQL acceptance for operation-key migration and retry replay."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from domain.tools import ToolExecutionRequest, ToolOperationClaimStatus
from infrastructure.persistence.postgres.models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    Base,
    PlanRoundModel,
    PlanVersionModel,
    RoundTaskModel,
    TaskExecutionModel,
    WorkerToolOperationModel,
)
from infrastructure.persistence.postgres.repositories.worker_tool_operation_repository import (
    PostgresWorkerToolOperationRepository,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_DIR = BACKEND_ROOT / "sql" / "migrations"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_migration_backfills_and_replays_across_attempts() -> None:
    """Backfill old ledger rows and replay a completed effect on retry."""
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"worker_operation_test_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    test_url = base_url.update_query_dict(
        {"options": f"-c search_path={schema}"}
    )
    test_engine = create_async_engine(test_url)
    created_schema = False
    now = datetime.now(timezone.utc)

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created_schema = True

        core_tables = [
            AgentRunModel.__table__,
            AgentPlanModel.__table__,
            AgentTaskModel.__table__,
            PlanVersionModel.__table__,
            PlanRoundModel.__table__,
            RoundTaskModel.__table__,
            TaskExecutionModel.__table__,
        ]
        async with test_engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection, tables=core_tables
                )
            )

        session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
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
                    user_goal="write a report",
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
            session.add_all(
                [
                    AgentTaskModel(
                        task_id="task-1",
                        plan_id="plan-1",
                        run_id="run-1",
                        title="Write report",
                        objective="write a report",
                        allowed_tools_json='["write_file"]',
                        timeout_seconds=3600,
                        status="running",
                        execution_generation=2,
                        active_execution_id="execution-2",
                        created_at=now.isoformat(),
                        updated_at=now.isoformat(),
                    ),
                    PlanVersionModel(
                        plan_id="plan-1",
                        version=1,
                        dag_json={},
                        created_at=now,
                    ),
                ]
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
                    active_execution_id="execution-2",
                )
            )
            await session.flush()
            session.add_all(
                [
                    TaskExecutionModel(
                        execution_id="execution-1",
                        round_id="round-1",
                        task_id="task-1",
                        attempt=1,
                        worker_thread_id="task:task-1:exec:1",
                        status="completed",
                        fencing_token=1,
                        cancellation_epoch=0,
                        created_at=now - timedelta(minutes=1),
                        updated_at=now - timedelta(minutes=1),
                    ),
                    TaskExecutionModel(
                        execution_id="execution-2",
                        round_id="round-1",
                        task_id="task-1",
                        attempt=2,
                        worker_thread_id="task:task-1:exec:2",
                        status="running",
                        lease_owner="integration-worker",
                        lease_until=now + timedelta(minutes=1),
                        fencing_token=2,
                        cancellation_epoch=0,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )

        old_ledger_sql = (
            MIGRATION_DIR / "20261022_worker_tool_operation_ledger.sql"
        ).read_text()
        async with test_engine.begin() as connection:
            for statement in old_ledger_sql.split(";"):
                if statement.strip():
                    await connection.exec_driver_sql(statement)
            await connection.execute(
                text("""
                    INSERT INTO worker_tool_operations (
                        operation_key, run_id, plan_id, task_id, execution_id,
                        tool_name, tool_call_id, args_hash, status, result_json,
                        created_at, completed_at
                    ) VALUES (
                        'athena:run-1:task-1:1:0:0', 'run-1', 'plan-1',
                        'task-1', 'execution-1', 'write_file',
                        'worker:task-1:1:0:0', 'args-hash', 'completed',
                        CAST(:result_json AS JSONB), :created_at, :completed_at
                    )
                """),
                {
                    "result_json": json.dumps(
                        {
                            "status": "success",
                            "output": "created",
                            "error": None,
                            "duration_ms": 12,
                        }
                    ),
                    "created_at": now - timedelta(minutes=1),
                    "completed_at": now - timedelta(minutes=1),
                },
            )

            logical_key_migration = (
                MIGRATION_DIR
                / "20261022_worker_tool_logical_operation_keys.sql"
            ).read_text()
            for statement in logical_key_migration.split(";"):
                if statement.strip():
                    await connection.exec_driver_sql(statement)
            backfilled = await connection.scalar(
                text(
                    "SELECT logical_operation_key FROM worker_tool_operations "
                    "WHERE operation_key = 'athena:run-1:task-1:1:0:0'"
                )
            )
            assert backfilled == "athena:run-1:task-1:logical:0:0"

        repository = PostgresWorkerToolOperationRepository(session_factory)
        retry_request = ToolExecutionRequest(
            tool_name="write_file",
            arguments={"path": "report.txt", "content": "ready"},
            session_id="session-1",
            run_id="run-1",
            tool_call_id="worker:task-1:2:0:0",
            agent_role="worker",
            plan_id="plan-1",
            task_id="task-1",
            operation_key="athena:run-1:task-1:2:0:0",
            logical_operation_key="athena:run-1:task-1:logical:0:0",
            execution_id="execution-2",
            fencing_token=2,
            cancellation_epoch=0,
        )

        claim = await repository.claim(retry_request, args_hash="args-hash")

        assert claim.status is ToolOperationClaimStatus.COMPLETED
        assert claim.result is not None
        assert claim.result.status == "success"
        assert claim.result.output == "created"

        concurrent_request = replace(
            retry_request,
            tool_call_id="worker:task-1:2:0:1",
            operation_key="athena:run-1:task-1:2:0:1",
            logical_operation_key="athena:run-1:task-1:logical:0:1",
        )
        concurrent_claims = await asyncio.gather(
            repository.claim(concurrent_request, args_hash="second-call-hash"),
            repository.claim(concurrent_request, args_hash="second-call-hash"),
        )
        assert sorted(item.status.value for item in concurrent_claims) == [
            ToolOperationClaimStatus.ACQUIRED.value,
            ToolOperationClaimStatus.IN_PROGRESS.value,
        ]
        async with session_factory() as session:
            operation_count = await session.scalar(
                select(func.count())
                .select_from(WorkerToolOperationModel)
                .where(
                    WorkerToolOperationModel.logical_operation_key
                    == concurrent_request.logical_operation_key
                )
            )
        assert operation_count == 1
    finally:
        await test_engine.dispose()
        if created_schema:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
