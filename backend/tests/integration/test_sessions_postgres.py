"""会话目录 PostgreSQL 迁移与仓储集成测试。"""

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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.auth.model import UserModel
from adapter.project.repository import SqlAlchemyProjectRepository
from adapter.session.model import SessionModel
from adapter.session.repository import SqlAlchemySessionRepository
from domain.project import Project, ProjectNotFoundError
from domain.session import (
    Session,
    SessionProjectLockedError,
    SessionScope,
)

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
ALEMBIC_INI = Path(__file__).resolve().parents[3] / "backend" / "alembic.ini"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="ATHENA_TEST_DATABASE_URL is not configured",
    ),
]


def _session(
    session_id: str,
    owner_id: str,
    *,
    project_id: str | None = None,
) -> Session:
    timestamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    return Session(
        id=session_id,
        owner_id=owner_id,
        title=None,
        project_id=project_id,
        last_message_sequence=0,
        created_at=timestamp,
        updated_at=timestamp,
        deleted_time=None,
    )


def test_session_migration_constraints_and_repository_round_trip() -> None:
    """在临时 schema 中执行真实迁移并验证会话归属与绑定规则。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        schema = f"athena_v2_session_test_{token_hex(8)}"
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
            async with test_engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == "20261105_0005"

            timestamp = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
            async with session_factory() as session:
                async with session.begin():
                    session.add_all(
                        [
                            UserModel(
                                id="user_1",
                                email="user_1@example.com",
                                password_hash="hash:test",
                                display_name=None,
                                status="active",
                                created_at=timestamp,
                                updated_at=timestamp,
                                deleted_time=None,
                            ),
                            UserModel(
                                id="user_2",
                                email="user_2@example.com",
                                password_hash="hash:test",
                                display_name=None,
                                status="active",
                                created_at=timestamp,
                                updated_at=timestamp,
                                deleted_time=None,
                            ),
                        ]
                    )

            project_repository = SqlAlchemyProjectRepository(session_factory)
            await project_repository.create(
                Project(
                    id="project_1",
                    owner_id="user_1",
                    name="用户一项目",
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            await project_repository.create(
                Project(
                    id="project_2",
                    owner_id="user_1",
                    name="用户一第二项目",
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            await project_repository.create(
                Project(
                    id="project_other",
                    owner_id="user_2",
                    name="用户二项目",
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )

            repository = SqlAlchemySessionRepository(session_factory)
            bound = await repository.create(
                _session("session_bound", "user_1", project_id="project_1")
            )
            await repository.create(_session("session_unbound", "user_1"))
            assert bound.project_id == "project_1"
            assert (
                await repository.get_visible(
                    SessionScope(session_id="session_bound", owner_id="user_1")
                )
                == bound
            )

            with pytest.raises(ProjectNotFoundError):
                await repository.create(
                    _session(
                        "session_wrong_owner",
                        "user_1",
                        project_id="project_other",
                    )
                )
            with pytest.raises(IntegrityError):
                async with session_factory() as session:
                    async with session.begin():
                        session.add(
                            SessionModel(
                                id="session_wrong_owner",
                                owner_id="user_1",
                                title=None,
                                project_id="project_other",
                                last_message_sequence=0,
                                created_at=timestamp,
                                updated_at=timestamp,
                                deleted_time=None,
                            )
                        )

            updated = await repository.update(
                SessionScope(session_id="session_unbound", owner_id="user_1"),
                update_title=True,
                title="绑定前改名",
                update_project=True,
                project_id="project_1",
                updated_at=timestamp,
            )
            assert updated is not None
            assert updated.title == "绑定前改名"
            assert updated.project_id == "project_1"
            project_sessions = await repository.list_active(
                "user_1",
                project_id="project_1",
                unbound=False,
                offset=0,
                limit=20,
            )
            assert [item.id for item in project_sessions.items] == [
                "session_unbound",
                "session_bound",
            ]

            async with session_factory() as session:
                async with session.begin():
                    row = await session.get(SessionModel, "session_unbound")
                    assert row is not None
                    row.last_message_sequence = 1

            with pytest.raises(SessionProjectLockedError):
                await repository.update(
                    SessionScope(session_id="session_unbound", owner_id="user_1"),
                    update_title=False,
                    title=None,
                    update_project=True,
                    project_id="project_2",
                    updated_at=timestamp,
                )

            assert await repository.soft_delete(
                "user_1",
                "session_bound",
                timestamp,
            )
            assert await repository.soft_delete(
                "user_1",
                "session_bound",
                timestamp,
            )
            assert (
                await repository.get_visible(
                    SessionScope(session_id="session_bound", owner_id="user_1")
                )
                is None
            )
            project_sessions = await repository.list_active(
                "user_1",
                project_id="project_1",
                unbound=False,
                offset=0,
                limit=20,
            )
            assert [item.id for item in project_sessions.items] == ["session_unbound"]
        finally:
            await test_engine.dispose()
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin_engine.dispose()

    asyncio.run(scenario())
