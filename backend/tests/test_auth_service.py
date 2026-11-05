"""认证应用服务测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event, Lock
from typing import Any

import pytest

from app.auth.rate_limit import FixedWindowRateLimiter
from app.auth.service import (
    DEFAULT_SESSION_SECONDS,
    REMEMBER_SESSION_SECONDS,
    AuthService,
)
from domain.auth import (
    AuthContext,
    AuthRepository,
    AuthSession,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidCurrentPasswordError,
    InvalidDisplayNameError,
    InvalidEmailError,
    InvalidPasswordError,
    PasswordHasher,
    RateLimitExceededError,
    User,
    UserStatus,
)
from infrastructure.security.password import Argon2PasswordHasher


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


class FakePasswordHasher(PasswordHasher):
    """快速、可观测的确定性密码哈希器。"""

    dummy_hash = "fake:dummy"

    def __init__(self) -> None:
        self.verify_calls: list[tuple[str, str]] = []

    def hash_password(self, password: str) -> str:
        return f"fake:{password}"

    def verify_password(self, password: str, password_hash: str) -> bool:
        self.verify_calls.append((password, password_hash))
        return password_hash != self.dummy_hash and password_hash == f"fake:{password}"


class InMemoryAuthRepository(AuthRepository):
    """用于服务测试的内存用户与会话仓储。"""

    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.sessions: dict[str, AuthSession] = {}

    async def create_user_with_session(
        self,
        user: User,
        auth_session: AuthSession,
    ) -> User:
        if any(existing.email == user.email for existing in self.users.values()):
            raise EmailAlreadyRegisteredError("email_exists")
        self.users[user.id] = user
        self.sessions[auth_session.token_hash] = auth_session
        return user

    async def get_user_by_email(self, email: str) -> User | None:
        return next(
            (user for user in self.users.values() if user.email == email),
            None,
        )

    async def create_session(self, auth_session: AuthSession) -> AuthSession:
        self.sessions[auth_session.token_hash] = auth_session
        return auth_session

    async def get_active_session(
        self,
        token_hash: str,
        now: datetime,
    ) -> AuthContext | None:
        auth_session = self.sessions.get(token_hash)
        if (
            auth_session is None
            or auth_session.revoked_time is not None
            or auth_session.expires_at <= now
        ):
            return None
        user = self.users.get(auth_session.user_id)
        if user is None or user.status is not UserStatus.ACTIVE or user.deleted_time is not None:
            return None
        return AuthContext(user=user, session_id=auth_session.id)

    async def revoke_session(
        self,
        token_hash: str,
        revoked_at: datetime,
    ) -> bool:
        auth_session = self.sessions.get(token_hash)
        if auth_session is None or auth_session.revoked_time is not None:
            return False
        self.sessions[token_hash] = replace(auth_session, revoked_time=revoked_at)
        return True

    async def change_password_and_revoke_sessions(
        self,
        user_id: str,
        password_hash: str,
        updated_at: datetime,
        *,
        keep_session_id: str,
        revoked_at: datetime,
    ) -> bool:
        user = self.users.get(user_id)
        if user is None or user.deleted_time is not None:
            return False
        self.users[user_id] = replace(
            user,
            password_hash=password_hash,
            updated_at=updated_at,
        )
        for token_hash, auth_session in tuple(self.sessions.items()):
            if (
                auth_session.user_id == user_id
                and auth_session.id != keep_session_id
                and auth_session.revoked_time is None
            ):
                self.sessions[token_hash] = replace(
                    auth_session,
                    revoked_time=revoked_at,
                )
        return True


class MutableClock:
    """可以推进的 UTC 时钟。"""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: Any) -> None:
        self.value += timedelta(**kwargs)


def _service(
    *,
    clock: MutableClock | None = None,
    login_limit: int = 5,
    register_limit: int = 10,
) -> tuple[AuthService, InMemoryAuthRepository, FakePasswordHasher, MutableClock]:
    repository = InMemoryAuthRepository()
    hasher = FakePasswordHasher()
    current_clock = clock or MutableClock(datetime(2026, 1, 1, tzinfo=UTC))
    service = AuthService(
        repository,
        hasher,
        clock=current_clock,
        user_id_factory=_incrementing_factory("usr_"),
        auth_session_id_factory=_incrementing_factory("ses_"),
        token_factory=_incrementing_factory("token_"),
        login_account_limiter=FixedWindowRateLimiter(
            limit=login_limit,
            window_seconds=60,
            clock=lambda: current_clock.value.timestamp(),
        ),
        login_ip_limiter=FixedWindowRateLimiter(
            limit=max(login_limit, 1),
            window_seconds=60,
            clock=lambda: current_clock.value.timestamp(),
        ),
        register_limiter=FixedWindowRateLimiter(
            limit=register_limit,
            window_seconds=60,
            clock=lambda: current_clock.value.timestamp(),
        ),
    )
    return service, repository, hasher, current_clock


def _incrementing_factory(prefix: str) -> Any:
    counter = 0

    def create() -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}{counter}"

    return create


def test_register_normalizes_email_and_never_returns_password_hash() -> None:
    async def scenario() -> None:
        service, repository, _, clock = _service()
        issued = await service.register(
            "  User@Example.COM ",
            "long-enough-password",
            display_name="  Athena  ",
        )

        assert issued.user.email == "user@example.com"
        assert issued.user.display_name == "Athena"
        assert issued.token == "token_1"
        assert issued.expires_at == clock.value + timedelta(seconds=DEFAULT_SESSION_SECONDS)
        assert issued.max_age_seconds == DEFAULT_SESSION_SECONDS
        stored = repository.users["usr_1"]
        assert stored.password_hash == "fake:long-enough-password"
        assert "password" not in issued.user.__dataclass_fields__

    _run(scenario())


def test_password_operation_does_not_block_loop_and_cancelled_work_keeps_slot() -> None:
    async def scenario() -> None:
        service, _, _, _ = _service()
        service._password_operation_slots = asyncio.Semaphore(1)
        started = [Event(), Event()]
        release = Event()
        start_lock = Lock()
        call_count = 0

        def slow_operation(value: str) -> str:
            nonlocal call_count
            with start_lock:
                operation_index = call_count
                call_count += 1
            started[operation_index].set()
            if operation_index == 0:
                release.wait(timeout=5)
            return value

        first = asyncio.create_task(service._run_password_operation(slow_operation, "first"))
        assert await asyncio.to_thread(started[0].wait, 2)

        # 阻塞中的密码计算不应妨碍事件循环，也不能因调用方取消而提前释放并发名额。
        loop_progressed = asyncio.Event()
        asyncio.get_running_loop().call_soon(loop_progressed.set)
        await asyncio.wait_for(loop_progressed.wait(), timeout=0.5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        second = asyncio.create_task(service._run_password_operation(slow_operation, "second"))
        await asyncio.sleep(0.02)
        assert not started[1].is_set()

        release.set()
        assert await asyncio.to_thread(started[1].wait, 2)
        assert await asyncio.wait_for(second, timeout=2) == "second"

    _run(scenario())


def test_register_rejects_invalid_input_and_duplicate_email() -> None:
    async def scenario() -> None:
        service, _, _, _ = _service()
        with pytest.raises(InvalidEmailError):
            await service.register("not-an-email", "long-enough-password")
        with pytest.raises(InvalidPasswordError):
            await service.register("user@example.com", "too-short")
        with pytest.raises(InvalidDisplayNameError):
            await service.register(
                "user@example.com",
                "long-enough-password",
                display_name="first\nsecond",
            )

        await service.register("user@example.com", "long-enough-password")
        with pytest.raises(EmailAlreadyRegisteredError):
            await service.register("user@example.com", "long-enough-password")

    _run(scenario())


def test_login_uses_dummy_hash_for_unknown_user_and_generic_error() -> None:
    async def scenario() -> None:
        service, _, hasher, _ = _service()
        with pytest.raises(InvalidCredentialsError):
            await service.login("missing@example.com", "long-enough-password")
        assert hasher.verify_calls[-1] == (
            "long-enough-password",
            hasher.dummy_hash,
        )

        await service.register("user@example.com", "long-enough-password")
        with pytest.raises(InvalidCredentialsError):
            await service.login("user@example.com", "wrong-password-value")

    _run(scenario())


def test_disabled_or_deleted_user_cannot_login() -> None:
    async def scenario() -> None:
        service, repository, _, _ = _service()
        await service.register("user@example.com", "long-enough-password")
        user = repository.users["usr_1"]

        repository.users["usr_1"] = replace(user, status=UserStatus.DISABLED)
        with pytest.raises(InvalidCredentialsError):
            await service.login("user@example.com", "long-enough-password")

        repository.users["usr_1"] = replace(
            user,
            deleted_time=user.updated_at,
        )
        with pytest.raises(InvalidCredentialsError):
            await service.login("user@example.com", "long-enough-password")

    _run(scenario())


def test_session_expiry_and_logout_are_enforced_server_side() -> None:
    async def scenario() -> None:
        service, _, _, clock = _service()
        issued = await service.register("user@example.com", "long-enough-password")

        context = await service.authenticate(issued.token)
        assert context is not None
        assert context.user.email == "user@example.com"

        clock.advance(seconds=DEFAULT_SESSION_SECONDS)
        assert await service.authenticate(issued.token) is None

        second_service, _, _, _ = _service()
        second_issued = await second_service.register(
            "user@example.com",
            "long-enough-password",
        )
        await second_service.logout(second_issued.token)
        assert await second_service.authenticate(second_issued.token) is None

    _run(scenario())


def test_login_remember_me_and_password_change_revokes_other_sessions() -> None:
    async def scenario() -> None:
        service, _, _, _ = _service()
        first = await service.register("user@example.com", "long-enough-password")
        second = await service.login(
            "user@example.com",
            "long-enough-password",
            remember_me=True,
        )
        assert second.max_age_seconds == REMEMBER_SESSION_SECONDS

        current = await service.authenticate(second.token)
        assert current is not None
        with pytest.raises(InvalidCurrentPasswordError):
            await service.change_password(
                current,
                "wrong-current-password",
                "new-long-enough-password",
            )

        await service.change_password(
            current,
            "long-enough-password",
            "new-long-enough-password",
        )
        assert await service.authenticate(first.token) is None
        assert await service.authenticate(second.token) is not None
        with pytest.raises(InvalidCredentialsError):
            await service.login("user@example.com", "long-enough-password")
        await service.login("user@example.com", "new-long-enough-password")

    _run(scenario())


def test_login_and_registration_rate_limits() -> None:
    async def scenario() -> None:
        login_service, _, _, _ = _service(login_limit=1)
        await login_service.register("user@example.com", "long-enough-password")
        with pytest.raises(InvalidCredentialsError):
            await login_service.login("user@example.com", "wrong-password-value")
        with pytest.raises(RateLimitExceededError):
            await login_service.login("user@example.com", "long-enough-password")

        register_service, _, _, _ = _service(register_limit=1)
        await register_service.register("first@example.com", "long-enough-password")
        with pytest.raises(RateLimitExceededError):
            await register_service.register("second@example.com", "long-enough-password")

    _run(scenario())


def test_login_account_limit_cannot_be_bypassed_by_rotating_client_keys() -> None:
    async def scenario() -> None:
        service, _, _, _ = _service(login_limit=1)
        await service.register("user@example.com", "long-enough-password")

        with pytest.raises(InvalidCredentialsError):
            await service.login(
                "user@example.com",
                "wrong-password-value",
                client_key="198.51.100.1",
            )
        with pytest.raises(RateLimitExceededError):
            await service.login(
                "user@example.com",
                "long-enough-password",
                client_key="198.51.100.2",
            )

    _run(scenario())


def test_argon2_hasher_produces_salted_hashes() -> None:
    hasher = Argon2PasswordHasher()
    first = hasher.hash_password("long-enough-password")
    second = hasher.hash_password("long-enough-password")

    assert first != second
    assert hasher.verify_password("long-enough-password", first) is True
    assert hasher.verify_password("wrong-password-value", first) is False
    assert hasher.verify_password("long-enough-password", hasher.dummy_hash) is False
