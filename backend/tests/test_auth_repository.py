"""认证 async SQLAlchemy 仓储测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pg_unit_harness import create_pg_unit_db
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.auth.repository import (
    SqlAlchemyAuthRepository,
    SqlAlchemyAuthTransactionGuard,
)
from domain.auth import (
    AuthSession,
    EmailAlreadyRegisteredError,
    User,
    UserRole,
    UserStatus,
)


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


async def _make_repository() -> tuple[
    Any,
    async_sessionmaker[AsyncSession],
    SqlAlchemyAuthRepository,
]:
    engine, session_factory = await create_pg_unit_db()
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


async def _seed_user_with_session(
    repository: SqlAlchemyAuthRepository,
    user: User,
    auth_session: AuthSession,
) -> User:
    """测试辅助函数：依次通过正式 create_user + create_session 接口写入用户与登录会话。"""
    created = await repository.create_user(user)
    await repository.create_session(auth_session)
    return created


def test_repository_creates_user_and_session() -> None:
    async def scenario() -> None:
        engine, _, repository = await _make_repository()
        try:
            user = _user("usr_1", "user@example.com")
            session = _session("ses_1", user.id, "1" * 64)

            created = await _seed_user_with_session(repository, user, session)

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
            await _seed_user_with_session(
                repository,
                user,
                _session("ses_1", user.id, "1" * 64),
            )

            with pytest.raises(EmailAlreadyRegisteredError):
                await _seed_user_with_session(
                    repository,
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
                await _seed_user_with_session(
                    repository,
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
            await _seed_user_with_session(repository, user, first)
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


def test_display_name_update_only_changes_name_and_requires_active_session() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            now = datetime.now(UTC)
            user = _user("usr_display", "display@example.com")
            active_session = _session(
                "ses_display",
                user.id,
                "f" * 64,
                expires_at=now + timedelta(days=7),
            )
            expired_session = _session(
                "ses_expired_display",
                user.id,
                "e" * 64,
                expires_at=now - timedelta(seconds=1),
            )
            await _seed_user_with_session(repository, user, active_session)
            await repository.create_session(expired_session)

            updated = await repository.update_display_name(
                user.id,
                active_session.id,
                "New Name",
                now,
            )
            assert updated is not None
            assert updated.display_name == "New Name"
            assert updated.password_hash == user.password_hash
            assert updated.role is user.role
            assert updated.status is user.status

            rejected = await repository.update_display_name(
                user.id,
                expired_session.id,
                None,
                now,
            )
            assert rejected is None
            assert user.email is not None
            stored = await repository.get_user_by_email(user.email)
            assert stored is not None
            assert stored.display_name == "New Name"

            async with session_factory() as db_session:
                async with db_session.begin():
                    session_row = await db_session.get(
                        AuthSessionModel,
                        active_session.id,
                    )
                    assert session_row is not None
                    session_row.revoked_time = now
            rejected_revoked = await repository.update_display_name(
                user.id,
                active_session.id,
                "Ignored",
                now,
            )
            assert rejected_revoked is None
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_revalidates_current_role_and_session_state() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            now = datetime(2026, 1, 2, tzinfo=UTC)
            user = _user("usr_role", "role@example.com")
            auth_session = _session("ses_role", user.id, "e" * 64)
            await _seed_user_with_session(repository, user, auth_session)

            async with session_factory() as db_session:
                async with db_session.begin():
                    row = await db_session.get(UserModel, user.id)
                    assert row is not None
                    row.role = UserRole.SUPER_ADMIN.value

            context = await repository.get_active_session(auth_session.token_hash, now)
            assert context is not None
            assert context.user.role is UserRole.SUPER_ADMIN
            assert (
                await repository.get_current_session_user(user.id, auth_session.id, now)
            ) == context.user

            assert await repository.revoke_session(auth_session.token_hash, now)
            assert await repository.get_current_session_user(user.id, auth_session.id, now) is None
        finally:
            await engine.dispose()

    _run(scenario())


def test_repository_promotes_only_active_non_deleted_users_idempotently() -> None:
    async def scenario() -> None:
        engine, _, repository = await _make_repository()
        try:
            timestamp = datetime(2026, 1, 2, tzinfo=UTC)
            active = _user("usr_active_promote", "promote@example.com")
            disabled = _user(
                "usr_disabled_promote",
                "disabled-promote@example.com",
                status=UserStatus.DISABLED,
            )
            deleted = _user("usr_deleted_promote", "deleted-promote@example.com", deleted=True)
            for user in (active, disabled, deleted):
                await _seed_user_with_session(
                    repository,
                    user,
                    _session(f"ses_{user.id}", user.id, (user.id * 64)[:64]),
                )

            assert active.email is not None
            assert disabled.email is not None
            assert deleted.email is not None
            promoted = await repository.promote_active_user_by_email(active.email, timestamp)
            assert promoted is not None and promoted.role is UserRole.SUPER_ADMIN
            assert (
                await repository.promote_active_user_by_email(
                    active.email,
                    timestamp + timedelta(days=1),
                )
                == promoted
            )
            assert await repository.promote_active_user_by_email(disabled.email, timestamp) is None
            assert await repository.promote_active_user_by_email(deleted.email, timestamp) is None
            assert (
                await repository.promote_active_user_by_email("missing@example.com", timestamp)
                is None
            )
        finally:
            await engine.dispose()

    _run(scenario())


def test_transaction_guard_checks_locked_user_and_session_state() -> None:
    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            now = datetime(2026, 1, 2, tzinfo=UTC)
            cases = (
                ("admin", UserStatus.ACTIVE, False, False),
                ("disabled", UserStatus.DISABLED, False, False),
                ("deleted", UserStatus.ACTIVE, True, False),
                ("revoked", UserStatus.ACTIVE, False, True),
                ("expired", UserStatus.ACTIVE, False, False),
                ("expires_after_lock", UserStatus.ACTIVE, False, False),
            )
            for suffix, status, deleted, revoked in cases:
                user = _user(
                    f"usr_guard_{suffix}",
                    f"guard-{suffix}@example.com",
                    status=status,
                    deleted=deleted,
                )
                auth_session = _session(
                    f"ses_guard_{suffix}",
                    user.id,
                    (suffix.encode().hex() * 64)[:64],
                    expires_at=(
                        now - timedelta(seconds=1)
                        if suffix == "expired"
                        else now + timedelta(seconds=1)
                        if suffix == "expires_after_lock"
                        else None
                    ),
                )
                if revoked:
                    auth_session = AuthSession(
                        id=auth_session.id,
                        user_id=auth_session.user_id,
                        token_hash=auth_session.token_hash,
                        expires_at=auth_session.expires_at,
                        revoked_time=now - timedelta(seconds=1),
                        created_at=auth_session.created_at,
                    )
                await _seed_user_with_session(repository, user, auth_session)
                if suffix == "admin":
                    async with session_factory() as db_session:
                        async with db_session.begin():
                            row = await db_session.get(UserModel, user.id)
                            assert row is not None
                            row.role = UserRole.SUPER_ADMIN.value
                statements: list[str] = []

                def record_statement(
                    _connection: Any,
                    _cursor: Any,
                    statement: str,
                    _parameters: Any,
                    _context: Any,
                    _many: bool,
                    statement_log: list[str] = statements,
                ) -> None:
                    statement_log.append(statement.lower())

                event.listen(
                    engine.sync_engine,
                    "before_cursor_execute",
                    record_statement,
                )

                def lock_time_clock(statement_log: list[str] = statements) -> datetime:
                    assert statement_log and "auth_sessions" in statement_log[-1]
                    return now + timedelta(seconds=2)

                async with session_factory() as db_session:
                    async with db_session.begin():
                        guard = SqlAlchemyAuthTransactionGuard(
                            db_session,
                            clock=(
                                lock_time_clock if suffix == "expires_after_lock" else lambda: now
                            ),
                        )
                        actual = await guard.lock_current_session_user(
                            user.id,
                            auth_session.id,
                        )
                        if suffix == "admin":
                            assert actual is not None and actual.role is UserRole.SUPER_ADMIN
                        else:
                            assert actual is None
                        assert (
                            await guard.lock_current_session_user(
                                "different-user",
                                auth_session.id,
                            )
                            is None
                        )
                        assert (
                            await guard.lock_current_session_user(
                                user.id,
                                "different-session",
                            )
                            is None
                        )
        finally:
            await engine.dispose()

    _run(scenario())


def test_password_change_rejects_revoked_expired_foreign_disabled_and_deleted_sessions() -> None:
    """改密写事务内复核账号状态与当前保留会话有效性，失效身份不得改密或撤销他人会话。"""

    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            now = datetime(2026, 1, 2, tzinfo=UTC)
            active = _user("usr_active_cp", "active-cp@example.com")
            other_user = _user("usr_other_cp", "other-cp@example.com")
            disabled = _user(
                "usr_disabled_cp",
                "disabled-cp@example.com",
                status=UserStatus.DISABLED,
            )
            deleted = _user("usr_deleted_cp", "deleted-cp@example.com", deleted=True)

            active_sess = _session("ses_active_1", active.id, "a" * 64)
            active_sibling = _session("ses_active_2", active.id, "b" * 64)
            revoked_sess = _session("ses_revoked", active.id, "c" * 64)
            expired_sess = _session(
                "ses_expired",
                active.id,
                "d" * 64,
                expires_at=now - timedelta(seconds=1),
            )
            other_sess = _session("ses_other_user", other_user.id, "e" * 64)
            disabled_sess = _session("ses_disabled", disabled.id, "f" * 64)
            deleted_sess = _session("ses_deleted", deleted.id, "g" * 64)

            await _seed_user_with_session(repository, active, active_sess)
            await repository.create_session(active_sibling)
            await repository.create_session(revoked_sess)
            await repository.create_session(expired_sess)
            await _seed_user_with_session(repository, other_user, other_sess)
            await _seed_user_with_session(repository, disabled, disabled_sess)
            await _seed_user_with_session(repository, deleted, deleted_sess)
            assert await repository.revoke_session("c" * 64, now) is True

            # 1. 已登出（已撤销）会话被拒绝
            assert (
                await repository.change_password_and_revoke_sessions(
                    active.id,
                    "hash:should-not-apply",
                    now,
                    keep_session_id=revoked_sess.id,
                    revoked_at=now,
                )
                is False
            )
            # 2. 已过期会话被拒绝
            assert (
                await repository.change_password_and_revoke_sessions(
                    active.id,
                    "hash:should-not-apply",
                    now,
                    keep_session_id=expired_sess.id,
                    revoked_at=now,
                )
                is False
            )
            # 3. 属于其他用户的会话被拒绝
            assert (
                await repository.change_password_and_revoke_sessions(
                    active.id,
                    "hash:should-not-apply",
                    now,
                    keep_session_id=other_sess.id,
                    revoked_at=now,
                )
                is False
            )
            # 4. 已禁用账号被拒绝
            assert (
                await repository.change_password_and_revoke_sessions(
                    disabled.id,
                    "hash:should-not-apply",
                    now,
                    keep_session_id=disabled_sess.id,
                    revoked_at=now,
                )
                is False
            )
            # 5. 已软删除账号被拒绝
            assert (
                await repository.change_password_and_revoke_sessions(
                    deleted.id,
                    "hash:should-not-apply",
                    now,
                    keep_session_id=deleted_sess.id,
                    revoked_at=now,
                )
                is False
            )

            # 所有拒绝分支均不得修改原密码或撤销其他有效会话。
            stored = await repository.get_user_by_email("active-cp@example.com")
            assert stored is not None and stored.password_hash == "hash:old-password"
            assert await repository.get_active_session("a" * 64, now) is not None
            assert await repository.get_active_session("b" * 64, now) is not None
            async with session_factory() as db_session:
                disabled_row = await db_session.get(UserModel, disabled.id)
                assert disabled_row is not None
                assert disabled_row.password_hash == "hash:old-password"
        finally:
            await engine.dispose()

    _run(scenario())


def test_verify_active_session_user_uses_lock_free_single_join() -> None:
    """无锁复核函数以单条 JOIN 校验活跃账号与有效会话，严禁生成 FOR SHARE / FOR UPDATE。"""
    from sqlalchemy.dialects import postgresql

    from adapter.auth.repository import verify_active_session_user

    pg_dialect = postgresql.dialect()  # type: ignore[no-untyped-call]

    async def scenario() -> None:
        engine, session_factory, repository = await _make_repository()
        try:
            now = datetime(2026, 1, 2, tzinfo=UTC)
            active = _user("usr_vf_active", "vf-active@example.com")
            disabled = _user(
                "usr_vf_disabled", "vf-disabled@example.com", status=UserStatus.DISABLED
            )
            deleted = _user("usr_vf_deleted", "vf-deleted@example.com", deleted=True)
            await repository.create_user(active)
            await repository.create_user(disabled)
            await repository.create_user(deleted)

            valid_sess = _session(
                "ses_vf_valid", active.id, "1" * 64, expires_at=now + timedelta(hours=1)
            )
            revoked_sess = AuthSession(
                id="ses_vf_revoked",
                user_id=active.id,
                token_hash="2" * 64,
                expires_at=now + timedelta(hours=1),
                revoked_time=now,
                created_at=now,
            )
            expired_sess = _session("ses_vf_expired", active.id, "3" * 64, expires_at=now)
            disabled_sess = _session(
                "ses_vf_disabled", disabled.id, "4" * 64, expires_at=now + timedelta(hours=1)
            )
            deleted_sess = _session(
                "ses_vf_deleted", deleted.id, "5" * 64, expires_at=now + timedelta(hours=1)
            )
            for item in (valid_sess, revoked_sess, expired_sess, disabled_sess, deleted_sess):
                await repository.create_session(item)

            pg_sqls: list[str] = []

            class _InspectingSession:
                def __init__(self, inner: Any) -> None:
                    self._inner = inner

                async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
                    if hasattr(statement, "compile"):
                        pg_sqls.append(str(statement.compile(dialect=pg_dialect)))
                    return await self._inner.scalar(statement, *args, **kwargs)

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._inner, name)

            async with session_factory() as db_session:
                wrapped = _InspectingSession(db_session)
                verified = await verify_active_session_user(
                    wrapped,  # type: ignore[arg-type]
                    active.id,
                    valid_sess.id,
                    clock=lambda: now,
                )
                assert verified is not None
                assert verified.id == active.id
                assert len(pg_sqls) == 1
                assert "JOIN auth_sessions" in pg_sqls[0]
                assert "FOR SHARE" not in pg_sqls[0]
                assert "FOR UPDATE" not in pg_sqls[0]

                # 撤销、过期、停用、删除与错配会话均拒绝
                assert (
                    await verify_active_session_user(
                        db_session, active.id, revoked_sess.id, clock=lambda: now
                    )
                    is None
                )
                assert (
                    await verify_active_session_user(
                        db_session, active.id, expired_sess.id, clock=lambda: now
                    )
                    is None
                )
                assert (
                    await verify_active_session_user(
                        db_session, disabled.id, disabled_sess.id, clock=lambda: now
                    )
                    is None
                )
                assert (
                    await verify_active_session_user(
                        db_session, deleted.id, deleted_sess.id, clock=lambda: now
                    )
                    is None
                )
                assert (
                    await verify_active_session_user(
                        db_session, disabled.id, valid_sess.id, clock=lambda: now
                    )
                    is None
                )
        finally:
            await engine.dispose()

    _run(scenario())
