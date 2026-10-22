"""PostgreSQL acceptance for schema migration tracking and replay safety."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from infrastructure.persistence.postgres.models import Base
from infrastructure.persistence.postgres.schema_migrations import (
    MIGRATION_DIRECTORY,
    apply_schema_migrations,
)


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_schema_migrations_are_tracked_and_repeatable() -> None:
    if not DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"schema_migration_{uuid4().hex}"
    base_url = make_url(DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    base_engine = create_async_engine(base_url)
    test_engine = create_async_engine(
        base_url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    schema_created = False

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True

        async with test_engine.begin() as connection:
            required_tables = [
                "agent_runs",
                "agent_plans",
                "agent_tasks",
                "agent_events",
                "approvals",
                "plan_versions",
                "plan_rounds",
                "round_tasks",
                "task_executions",
                "task_execution_results",
                "orchestration_commands",
                "orchestration_outbox",
                "orchestration_inbox",
                "worker_jobs",
                "root_resume_jobs",
                "worker_tool_operations",
            ]
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[Base.metadata.tables[name] for name in required_tables],
                )
            )
            await connection.run_sync(apply_schema_migrations)
            await connection.run_sync(apply_schema_migrations)
            applied_count = await connection.scalar(
                text("SELECT count(*) FROM athena_schema_migrations")
            )
            expected_count = len(list(MIGRATION_DIRECTORY.glob("*.sql")))
            approval_columns = await connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'approvals'"
                )
            )
            approval_column_names = {row[0] for row in approval_columns}

        assert applied_count == expected_count
        assert "execution_id" in approval_column_names
    finally:
        await test_engine.dispose()
        if schema_created:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base_engine.dispose()
