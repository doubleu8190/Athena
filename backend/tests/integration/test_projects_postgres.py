"""项目 PostgreSQL 迁移与仓储集成测试。"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import pytest
from integration.postgres_harness import isolated_postgres_schema
from sqlalchemy import text

from adapter.auth.model import UserModel
from adapter.project.repository import SqlAlchemyProjectRepository
from domain.project import Project

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="ATHENA_TEST_DATABASE_URL is not configured",
    ),
]


def test_repository_round_trip_after_real_alembic_migration() -> None:
    """在临时 schema 中执行真实迁移并验证仓储软删除查询。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        async with isolated_postgres_schema(database_url, prefix="athena_v2_test") as db:
            expected_head = db.head_revision()
            session_factory = db.session_factory
            repository = SqlAlchemyProjectRepository(session_factory)
            async with db.engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == expected_head

            timestamp = datetime(2026, 1, 1, tzinfo=UTC)
            async with session_factory() as session:
                async with session.begin():
                    session.add(
                        UserModel(
                            id="user_1",
                            email="user_1@example.com",
                            password_hash="hash:test",
                            display_name=None,
                            status="active",
                            created_at=timestamp,
                            updated_at=timestamp,
                            deleted_time=None,
                        )
                    )
            project = Project(
                id="project_postgres",
                owner_id="user_1",
                name="PostgreSQL 项目",
                created_at=timestamp,
                updated_at=timestamp,
            )

            await repository.create(project)
            assert (await repository.get("user_1", project.id)) == project
            assert await repository.get("user_2", project.id) is None
            page = await repository.list_active("user_1", offset=0, limit=20)
            assert page.total == 1
            other_page = await repository.list_active("user_2", offset=0, limit=20)
            assert other_page.total == 0

            assert (
                await repository.soft_delete(
                    "user_1",
                    project.id,
                    timestamp,
                )
                is True
            )
            assert await repository.get("user_1", project.id) is None

    asyncio.run(scenario())
