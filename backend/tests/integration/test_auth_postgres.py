"""认证 PostgreSQL 迁移与仓储集成测试。"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from integration.postgres_harness import isolated_postgres_schema
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.project.model import ProjectModel
from adapter.session.model import SessionModel
from domain.auth import (
    AuthSession,
    EmailAlreadyRegisteredError,
    User,
    UserRole,
    UserStatus,
)

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="ATHENA_TEST_DATABASE_URL is not configured",
    ),
]


def test_auth_migration_and_repository_with_owned_resources() -> None:
    """在临时 schema 中验证认证表、外键和会话撤销。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    async def scenario() -> None:
        async with isolated_postgres_schema(database_url, prefix="athena_v2_auth_test") as db:
            test_engine = db.engine
            expected_head = db.head_revision()

            async with test_engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert revision == expected_head

            session_factory = db.session_factory
            repository = SqlAlchemyAuthRepository(session_factory)
            timestamp = datetime(2026, 1, 1, tzinfo=UTC)
            user = User(
                id="usr_postgres",
                email="postgres@example.com",
                password_hash="hash:old-password",
                display_name="Postgres",
                status=UserStatus.ACTIVE,
                created_at=timestamp,
                updated_at=timestamp,
            )
            first = AuthSession(
                id="ses_postgres_1",
                user_id=user.id,
                token_hash="1" * 64,
                expires_at=timestamp + timedelta(days=7),
                revoked_time=None,
                created_at=timestamp,
            )
            second = AuthSession(
                id="ses_postgres_2",
                user_id=user.id,
                token_hash="2" * 64,
                expires_at=timestamp + timedelta(days=7),
                revoked_time=None,
                created_at=timestamp,
            )
            await repository.create_user_with_session(user, first)
            assert user.role is UserRole.MEMBER
            await repository.create_session(second)
            assert user.email is not None
            promoted = await repository.promote_active_user_by_email(user.email, timestamp)
            assert promoted is not None and promoted.role is UserRole.SUPER_ADMIN
            assert (
                await repository.get_current_session_user(user.id, first.id, timestamp)
            ) == promoted

            with pytest.raises(EmailAlreadyRegisteredError):
                await repository.create_user_with_session(
                    User(
                        id="usr_duplicate",
                        email=user.email,
                        password_hash="hash:duplicate",
                        display_name=None,
                        status=UserStatus.ACTIVE,
                        created_at=timestamp,
                        updated_at=timestamp,
                    ),
                    AuthSession(
                        id="ses_duplicate",
                        user_id="usr_duplicate",
                        token_hash="3" * 64,
                        expires_at=timestamp + timedelta(days=7),
                        revoked_time=None,
                        created_at=timestamp,
                    ),
                )

            async with session_factory() as session:
                async with session.begin():
                    session.add(
                        ProjectModel(
                            id="project_postgres",
                            owner_id=user.id,
                            name="PostgreSQL 项目",
                            created_at=timestamp,
                            updated_at=timestamp,
                            deleted_time=None,
                        )
                    )
                    session.add(
                        SessionModel(
                            id="session_postgres",
                            owner_id=user.id,
                            last_message_sequence=0,
                            created_at=timestamp,
                            updated_at=timestamp,
                            deleted_time=None,
                        )
                    )

            async with session_factory() as session:
                project_owner = await session.scalar(
                    select(ProjectModel.owner_id).where(ProjectModel.id == "project_postgres")
                )
                session_owner = await session.scalar(
                    select(SessionModel.owner_id).where(SessionModel.id == "session_postgres")
                )
            assert project_owner == user.id
            assert session_owner == user.id

            changed = await repository.change_password_and_revoke_sessions(
                user.id,
                "hash:new-password",
                timestamp,
                keep_session_id=second.id,
                revoked_at=timestamp,
            )
            assert changed is True
            assert await repository.get_active_session("1" * 64, timestamp) is None
            assert await repository.get_active_session("2" * 64, timestamp) is not None

            async def create_duplicate(index: int) -> User:
                candidate = User(
                    id=f"usr_concurrent_{index}",
                    email="concurrent@example.com",
                    password_hash=f"hash:{index}",
                    display_name=None,
                    status=UserStatus.ACTIVE,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                return await repository.create_user_with_session(
                    candidate,
                    AuthSession(
                        id=f"ses_concurrent_{index}",
                        user_id=candidate.id,
                        token_hash=str(index) * 64,
                        expires_at=timestamp + timedelta(days=7),
                        revoked_time=None,
                        created_at=timestamp,
                    ),
                )

            concurrent_results = await asyncio.gather(
                create_duplicate(4),
                create_duplicate(5),
                return_exceptions=True,
            )
            assert sum(isinstance(item, User) for item in concurrent_results) == 1
            assert (
                sum(isinstance(item, EmailAlreadyRegisteredError) for item in concurrent_results)
                == 1
            )

            await db.downgrade_to("20261105_0005")
            await db.upgrade_to("head")
            async with test_engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text("SELECT role FROM users WHERE id = :user_id"),
                        {"user_id": user.id},
                    )
                    == UserRole.MEMBER.value
                )
            with pytest.raises(IntegrityError):
                async with test_engine.begin() as connection:
                    await connection.execute(
                        text("UPDATE users SET role = 'owner' WHERE id = :user_id"),
                        {"user_id": user.id},
                    )

    asyncio.run(scenario())
