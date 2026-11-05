"""项目 PostgreSQL 迁移与仓储集成测试。"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.auth.model import UserModel
from adapter.project.repository import SqlAlchemyProjectRepository
from domain.project import Project

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
ALEMBIC_INI = Path(__file__).resolve().parents[3] / "backend" / "alembic.ini"
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
        schema = f"athena_v2_test_{token_hex(8)}"
        admin_engine = create_async_engine(database_url, poolclass=NullPool)
        test_engine = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema}},
        )
        try:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

            migration_config = Config(str(ALEMBIC_INI))
            migration_config.set_main_option(
                "sqlalchemy.url",
                database_url.replace("%", "%%"),
            )
            migration_config.attributes["connect_args"] = {
                "server_settings": {"search_path": schema}
            }
            await asyncio.to_thread(command.upgrade, migration_config, "head")

            session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
            repository = SqlAlchemyProjectRepository(session_factory)
            async with test_engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "20261105_0005"

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
        finally:
            await test_engine.dispose()
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin_engine.dispose()

    asyncio.run(scenario())
