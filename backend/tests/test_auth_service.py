"""认证应用服务测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event, Lock
from typing import Any

import pytest
from auth_support import InMemoryAuthRepository

from app.auth.rate_limit import FixedWindowRateLimiter
from app.auth.service import (
    DEFAULT_SESSION_SECONDS,
    REMEMBER_SESSION_SECONDS,
    AuthService,
)
from domain.auth import (
    AdminPromotionError,
    AuthProvider,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidCurrentPasswordError,
    InvalidDisplayNameError,
    InvalidEmailError,
    InvalidPasswordError,
    PasswordChangeNotSupportedError,
    PasswordHasher,
    RateLimitExceededError,
    User,
    UserRole,
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


class MutableClock:
    """可以推进的 UTC 时钟。"""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: Any) -> None:
        self.value += timedelta(**kwargs)


def _incrementing_factory(prefix: str) -> Any:
    counter = 0

    def create() -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}{counter}"

    return create


def _service(
    *,
    clock: MutableClock | None = None,
    login_limit: int = 5,
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
    )
    return service, repository, hasher, current_clock


def test_provision_local_user_normalizes_email_and_never_returns_password_hash() -> None:
    async def scenario() -> None:
        service, repository, _, _ = _service()
        created = await service.provision_local_user(
            "  User@Example.COM ",
            "long-enough-password",
            display_name="  Athena  ",
        )

        assert created.email == "user@example.com"
        assert created.display_name == "Athena"
        assert created.role is UserRole.MEMBER
        assert created.auth_provider is AuthProvider.LOCAL
        stored = repository.users["usr_1"]
        assert stored.password_hash == "fake:long-enough-password"
        assert "password" not in created.__dataclass_fields__

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


def test_provision_local_user_rejects_invalid_input_and_duplicate_email() -> None:
    async def scenario() -> None:
        service, _, _, _ = _service()
        with pytest.raises(InvalidEmailError):
            await service.provision_local_user("not-an-email", "long-enough-password")
        with pytest.raises(InvalidPasswordError):
            await service.provision_local_user("user@example.com", "too-short")
        with pytest.raises(InvalidDisplayNameError):
            await service.provision_local_user(
                "user@example.com",
                "long-enough-password",
                display_name="first\nsecond",
            )

        await service.provision_local_user("user@example.com", "long-enough-password")
        with pytest.raises(EmailAlreadyRegisteredError):
            await service.provision_local_user("user@example.com", "long-enough-password")

    _run(scenario())


def test_bootstrap_super_admin_creates_resets_and_rejects_external_provider() -> None:
    """应急 super_admin 创建、密码重置撤销旧会话，且拒绝覆盖外部 SSO 账号。"""

    async def scenario() -> None:
        service, repository, _, _ = _service()
        admin, created = await service.bootstrap_super_admin(
            "Admin@Example.com",
            "initial-super-password",
            display_name="应急超管",
        )
        assert created is True
        assert admin.role is UserRole.SUPER_ADMIN
        assert admin.auth_provider is AuthProvider.LOCAL
        assert admin.email == "admin@example.com"

        issued = await service.login("admin@example.com", "initial-super-password")
        assert await service.authenticate(issued.token) is not None

        reset_admin, created_again = await service.bootstrap_super_admin(
            "admin@example.com",
            "rotated-super-password",
        )
        assert created_again is False
        assert reset_admin.id == admin.id
        # 密码重置必须立即撤销旧会话，旧密码不可再登录，新密码可正常登录。
        assert await service.authenticate(issued.token) is None
        with pytest.raises(InvalidCredentialsError):
            await service.login("admin@example.com", "initial-super-password")
        relogged = await service.login("admin@example.com", "rotated-super-password")
        assert relogged.user.role is UserRole.SUPER_ADMIN

        # 外部飞书账号占用邮箱时，拒绝通过 bootstrap 劫持。
        now = datetime(2026, 1, 1, tzinfo=UTC)
        repository.users["usr_feishu"] = User(
            id="usr_feishu",
            email="feishu-boss@example.com",
            password_hash=None,
            display_name="飞书老板",
            status=UserStatus.ACTIVE,
            role=UserRole.MEMBER,
            auth_provider=AuthProvider.FEISHU,
            created_at=now,
            updated_at=now,
        )
        with pytest.raises(AdminPromotionError):
            await service.bootstrap_super_admin("feishu-boss@example.com", "another-long-password")

    _run(scenario())


def test_update_display_name_normalizes_clear_and_rejects_invalid_values() -> None:
    async def scenario() -> None:
        service, repository, _, _ = _service()
        await service.provision_local_user(
            "user@example.com",
            "long-enough-password",
            display_name="Original",
        )
        issued = await service.login("user@example.com", "long-enough-password")
        context = await service.authenticate(issued.token)
        assert context is not None
        original_password_hash = repository.users[context.user.id].password_hash

        updated = await service.update_display_name(context, "  New Name  ")
        assert updated.display_name == "New Name"
        assert repository.users[updated.id].password_hash == original_password_hash

        cleared = await service.update_display_name(context, " ")
        assert cleared.display_name is None
        with pytest.raises(InvalidDisplayNameError):
            await service.update_display_name(context, "bad\nname")

    _run(scenario())


def test_login_uses_dummy_hash_for_unknown_and_non_local_users() -> None:
    """未知账号、飞书账号、Mock 账号或无密码哈希账号登录时均执行 dummy_hash 校验防枚举。"""

    async def scenario() -> None:
        service, repository, hasher, _ = _service()
        with pytest.raises(InvalidCredentialsError):
            await service.login("missing@example.com", "long-enough-password")
        assert hasher.verify_calls[-1] == ("long-enough-password", hasher.dummy_hash)

        now = datetime(2026, 1, 1, tzinfo=UTC)
        for provider, email in (
            (AuthProvider.FEISHU, "feishu@example.com"),
            (AuthProvider.MOCK, "mock@example.com"),
        ):
            repository.users[f"usr_{provider.value}"] = User(
                id=f"usr_{provider.value}",
                email=email,
                password_hash=None,
                display_name=provider.value,
                status=UserStatus.ACTIVE,
                role=UserRole.MEMBER,
                auth_provider=provider,
                created_at=now,
                updated_at=now,
            )
            with pytest.raises(InvalidCredentialsError):
                await service.login(email, "long-enough-password")
            assert hasher.verify_calls[-1] == ("long-enough-password", hasher.dummy_hash)

        await service.provision_local_user("user@example.com", "long-enough-password")
        with pytest.raises(InvalidCredentialsError):
            await service.login("user@example.com", "wrong-password-value")

    _run(scenario())


def test_disabled_or_deleted_user_cannot_login() -> None:
    async def scenario() -> None:
        service, repository, _, _ = _service()
        await service.provision_local_user("user@example.com", "long-enough-password")
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
        await service.provision_local_user("user@example.com", "long-enough-password")
        issued = await service.login("user@example.com", "long-enough-password")

        context = await service.authenticate(issued.token)
        assert context is not None
        assert context.user.email == "user@example.com"

        clock.advance(seconds=DEFAULT_SESSION_SECONDS)
        assert await service.authenticate(issued.token) is None

        second_service, _, _, _ = _service()
        await second_service.provision_local_user("user@example.com", "long-enough-password")
        second_issued = await second_service.login("user@example.com", "long-enough-password")
        await second_service.logout(second_issued.token)
        assert await second_service.authenticate(second_issued.token) is None

    _run(scenario())


def test_login_remember_me_and_password_change_revokes_other_sessions() -> None:
    async def scenario() -> None:
        service, _, _, _ = _service()
        await service.provision_local_user("user@example.com", "long-enough-password")
        first = await service.login("user@example.com", "long-enough-password")
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


def test_change_password_rejects_sso_and_mock_users() -> None:
    """飞书与 Mock 账号调用 change_password 直接抛出 PasswordChangeNotSupportedError。"""

    async def scenario() -> None:
        service, repository, _, clock = _service()
        now = clock()
        for provider in (AuthProvider.FEISHU, AuthProvider.MOCK):
            user = User(
                id=f"usr_{provider.value}",
                email=f"{provider.value}@example.com",
                password_hash=None,
                display_name=provider.value,
                status=UserStatus.ACTIVE,
                role=UserRole.MEMBER,
                auth_provider=provider,
                created_at=now,
                updated_at=now,
            )
            repository.users[user.id] = user
            auth_session, token, _ = service._new_session(user.id, now, DEFAULT_SESSION_SECONDS)
            await repository.create_session(auth_session)
            ctx = await service.authenticate(token)
            assert ctx is not None
            with pytest.raises(PasswordChangeNotSupportedError):
                await service.change_password(
                    ctx,
                    "long-enough-password",
                    "new-long-enough-password",
                )

    _run(scenario())


def test_login_rate_limits_and_account_limit_cannot_be_bypassed() -> None:
    async def scenario() -> None:
        login_service, _, _, _ = _service(login_limit=1)
        await login_service.provision_local_user("user@example.com", "long-enough-password")
        with pytest.raises(InvalidCredentialsError):
            await login_service.login(
                "user@example.com",
                "wrong-password-value",
                client_key="198.51.100.1",
            )
        with pytest.raises(RateLimitExceededError):
            await login_service.login(
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


def test_admin_promotion_is_idempotent_and_requires_active_existing_account() -> None:
    async def scenario() -> None:
        service, repository, _, _ = _service()
        provisioned = await service.provision_local_user(
            "admin@example.com",
            "long-enough-password",
        )
        promoted = await service.promote_admin_by_email("ADMIN@example.com")
        assert promoted.role is UserRole.SUPER_ADMIN
        assert await service.promote_admin_by_email("admin@example.com") == promoted
        assert provisioned.role is UserRole.MEMBER

        repository.users["usr_2"] = replace(
            repository.users["usr_1"],
            id="usr_2",
            email="disabled@example.com",
            status=UserStatus.DISABLED,
        )
        repository.users["usr_3"] = replace(
            repository.users["usr_1"],
            id="usr_3",
            email="deleted@example.com",
            deleted_time=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for email in ("missing@example.com", "disabled@example.com", "deleted@example.com"):
            with pytest.raises(AdminPromotionError):
                await service.promote_admin_by_email(email)

    _run(scenario())


def test_admin_revalidation_reads_current_role_and_rejects_revoked_session() -> None:
    async def scenario() -> None:
        service, repository, _, _ = _service()
        await service.provision_local_user("user@example.com", "long-enough-password")
        issued = await service.login("user@example.com", "long-enough-password")
        context = await service.authenticate(issued.token)
        assert context is not None
        assert await service.revalidate_admin(context) is None

        # dept_admin 不得通过 super_admin 复核。
        repository.users[context.user.id] = replace(context.user, role=UserRole.DEPT_ADMIN)
        assert await service.revalidate_admin(context) is None

        repository.users[context.user.id] = replace(context.user, role=UserRole.SUPER_ADMIN)
        current = await service.revalidate_admin(context)
        assert current is not None and current.role is UserRole.SUPER_ADMIN

        await service.logout(issued.token)
        assert await service.revalidate_admin(context) is None

    _run(scenario())


def test_change_password_rate_limits_failures_and_resets_on_success() -> None:
    """改密按 user_id 限制连续失败次数（默认 5 次触发限流），改密成功后重置计数。"""

    async def scenario() -> None:
        service, _, _, _ = _service()
        await service.provision_local_user("user@example.com", "long-enough-password")
        issued = await service.login("user@example.com", "long-enough-password")
        context = await service.authenticate(issued.token)
        assert context is not None

        for _ in range(4):
            with pytest.raises(InvalidCurrentPasswordError):
                await service.change_password(
                    context,
                    "wrong-current-password",
                    "next-long-enough-password",
                )
        await service.change_password(
            context,
            "long-enough-password",
            "next-long-enough-password",
        )

        refreshed = await service.authenticate(issued.token)
        assert refreshed is not None
        for _ in range(5):
            with pytest.raises(InvalidCurrentPasswordError):
                await service.change_password(
                    refreshed,
                    "wrong-current-password",
                    "third-long-enough-password",
                )
        with pytest.raises(RateLimitExceededError):
            await service.change_password(
                refreshed,
                "next-long-enough-password",
                "third-long-enough-password",
            )

    _run(scenario())
