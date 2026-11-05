"""认证 async SQLAlchemy 仓储测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.models import Base
from domain.auth import (
    AuthSession,
    EmailAlreadyRegisteredError,
    User,
    UserStatus,
)


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


async def _make_repository() -> tuple[
    Any,
    async_sessionmaker[AsyncSession],
    SqlAlchemyAuthRepository,
]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_factory, SqlAlchemyAuthRepository(session_factory)


def _user(
    user_id: str,
    email: str,
    *,
    status: UserStatus = UserStatus.ACTIVE,
    deleted: bool = False,
) -> User:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    return User(
        id=user_id,
        email=email,
        password_hash="hash:old-password",
        display_name=None,
        status=status,
        created_at=timestamp,
        updated_at=timestamp,
        deleted_time=timestamp if deleted else None,
    )


def _session(
    session_id: str,
    user_id: str,
    token_hash: str,
    *,
    expires_at: datetime | None = None,
) -> AuthSession:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    return AuthSession(
        id=session_id,
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at or timestamp + timedelta(days=7),
        revoked_time=None,
        created_at=timestamp,
    )


def test_repository_creates_user_and_session_atomically() -> None:
    async def scenario() -> None:
        engine, _, repository = await _make_repository()
        try:
            user = _user("usr_1", "user@example.com")
            session = _session("ses_1", user.id, "1" * 64)

            created = await repository.create_user_with_session(user, session)

            assert created == user
            assert await repository.get_user_by_email("user@example.com") == user
            context = await repository.get_active_session(
                session.token_hash,
                datetime(2026, 1, 2, tzinfo=UTC),
            )
            assert context is not None
            assert context.user == user
            assert context.session_id == "ses_1"
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_rejects_duplicate_email_once() -> None:
    async def scenario() -> None:
        engine, _, repository = await _make_repository()
        try:
            user = _user("usr_1", "user@example.com")
            await repository.create_user_with_session(
                user,
                _session("ses_1", user.id, "1" * 64),
            )

            with pytest.raises(EmailAlreadyRegisteredError):
                await repository.create_user_with_session(
                    _user("usr_2", "user@example.com"),
                    _session("ses_2", "usr_2", "2" * 64),
                )
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_hides_disabled_deleted_expired_and_revoked_sessions() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            now = datetime(2026, 1, 2, tzinfo=UTC)
            active = _user("usr_active", "active@example.com")
            disabled = _user(
                "usr_disabled",
                "disabled@example.com",
                status=UserStatus.DISABLED,
            )
            deleted = _user(
                "usr_deleted",
                "deleted@example.com",
                deleted=True,
            )
            for user, token in (
                (active, "a" * 64),
                (disabled, "b" * 64),
                (deleted, "c" * 64),
            ):
                await repository.create_user_with_session(
                    user,
                    _session(f"ses_{user.id}", user.id, token),
                )

            assert await repository.get_active_session("a" * 64, now) is not None
            assert await repository.get_active_session("b" * 64, now) is None
            assert await repository.get_active_session("c" * 64, now) is None

            expired = _session(
                "ses_expired",
                active.id,
                "d" * 64,
                expires_at=now - timedelta(seconds=1),
            )
            await repository.create_session(expired)
            assert await repository.get_active_session("d" * 64, now) is None

            assert await repository.revoke_session("a" * 64, now) is True
            assert await repository.get_active_session("a" * 64, now) is None

            async with session_factory() as db_session:
                assert await db_session.get(UserModel, active.id) is not None
                assert await db_session.get(AuthSessionModel, "ses_usr_active") is not None
        finally:
            await engine.dispose()

    _run(scenario())


def test_password_change_revokes_other_sessions_and_keeps_current() -> None:
    async def scenario() -> None:
        engine, _, repository = await _make_repository()
        try:
            user = _user("usr_1", "user@example.com")
            first = _session("ses_1", user.id, "1" * 64)
            second = _session("ses_2", user.id, "2" * 64)
            third = _session("ses_3", user.id, "3" * 64)
            await repository.create_user_with_session(user, first)
            await repository.create_session(second)
            await repository.create_session(third)
            changed_at = datetime(2026, 1, 2, tzinfo=UTC)

            changed = await repository.change_password_and_revoke_sessions(
                user.id,
                "hash:new-password",
                changed_at,
                keep_session_id=second.id,
                revoked_at=changed_at,
            )

            assert changed is True
            stored = await repository.get_user_by_email("user@example.com")
            assert stored is not None
            assert stored.password_hash == "hash:new-password"
            assert stored.updated_at == changed_at
            assert await repository.get_active_session("1" * 64, changed_at) is None
            assert await repository.get_active_session("2" * 64, changed_at) is not None
            assert await repository.get_active_session("3" * 64, changed_at) is None
        finally:
            await engine.dispose()

    _run(scenario())
