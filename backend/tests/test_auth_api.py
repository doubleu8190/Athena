"""认证 HTTP API 测试。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import AuthCookieConfig, build_auth_router
from api.security import AdminAuth
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
    PasswordHasher,
    User,
    UserRole,
    UserStatus,
)


class FakePasswordHasher(PasswordHasher):
    """快速确定性密码哈希器。"""

    dummy_hash = "fake:dummy"

    def hash_password(self, password: str) -> str:
        return f"fake:{password}"

    def verify_password(self, password: str, password_hash: str) -> bool:
        return password_hash != self.dummy_hash and password_hash == f"fake:{password}"


class InMemoryAuthRepository(AuthRepository):
    """API 测试使用的内存认证仓储。"""

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

    async def get_current_session_user(
        self,
        user_id: str,
        session_id: str,
        now: datetime,
    ) -> User | None:
        session = next((item for item in self.sessions.values() if item.id == session_id), None)
        user = self.users.get(user_id)
        if session is None or session.user_id != user_id or session.revoked_time is not None:
            return None
        if session.expires_at <= now or user is None:
            return None
        if user.status is not UserStatus.ACTIVE or user.deleted_time is not None:
            return None
        return user

    async def promote_active_user_by_email(
        self,
        email: str,
        updated_at: datetime,
    ) -> User | None:
        user = await self.get_user_by_email(email)
        if user is None or user.status is not UserStatus.ACTIVE or user.deleted_time is not None:
            return None
        promoted = replace(user, role=UserRole.ADMIN, updated_at=updated_at)
        self.users[user.id] = promoted
        return promoted

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

    async def update_display_name(
        self,
        user_id: str,
        session_id: str,
        display_name: str | None,
        updated_at: datetime,
    ) -> User | None:
        user = self.users.get(user_id)
        session = next((item for item in self.sessions.values() if item.id == session_id), None)
        if (
            user is None
            or session is None
            or session.user_id != user_id
            or session.revoked_time is not None
            or session.expires_at <= updated_at
        ):
            return None
        updated = replace(user, display_name=display_name, updated_at=updated_at)
        self.users[user_id] = updated
        return updated


def _incrementing_factory(prefix: str) -> Callable[[], str]:
    counter = 0

    def create() -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}{counter}"

    return create


def _service(*, login_limit: int = 5) -> AuthService:
    return AuthService(
        InMemoryAuthRepository(),
        FakePasswordHasher(),
        user_id_factory=_incrementing_factory("usr_"),
        auth_session_id_factory=_incrementing_factory("ses_"),
        token_factory=_incrementing_factory("token_"),
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        login_account_limiter=FixedWindowRateLimiter(
            limit=login_limit,
            window_seconds=60,
            clock=lambda: 0.0,
        ),
        login_ip_limiter=FixedWindowRateLimiter(
            limit=login_limit,
            window_seconds=60,
            clock=lambda: 0.0,
        ),
    )


def _app(
    service: AuthService,
    *,
    secure: bool = False,
    public_origin: str | None = None,
) -> FastAPI:
    app = FastAPI()
    app.state.authenticator = service
    app.state.auth_cookie_name = "athena_session"
    app.state.public_origin = public_origin
    app.include_router(
        build_auth_router(
            service,
            cookie=AuthCookieConfig(secure=secure),
        )
    )
    return app


def _register(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "user@example.com",
            "password": "long-enough-password",
            "display_name": "Athena",
        },
    )
    assert response.status_code == 201


def test_register_sets_http_only_cookie_and_me_returns_user() -> None:
    service = _service()
    with TestClient(_app(service)) as client:
        response = client.post(
            "/api/v1/auth/register",
            json={
                "email": " User@Example.COM ",
                "password": "long-enough-password",
                "display_name": "Athena",
            },
        )
        cookie = response.headers["set-cookie"].lower()
        me = client.get("/api/v1/auth/me")

    assert response.status_code == 201
    assert response.json()["email"] == "user@example.com"
    assert response.json()["display_name"] == "Athena"
    assert response.json()["role"] == "user"
    assert "password" not in response.json()
    assert "password_hash" not in response.json()
    assert "status" not in response.json()
    assert "deleted_time" not in response.json()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert f"max-age={DEFAULT_SESSION_SECONDS}" in cookie
    assert response.headers["cache-control"] == "no-store"
    assert me.status_code == 200
    assert me.json()["id"] == response.json()["id"]
    assert me.json()["role"] == "user"


def test_update_current_display_name_requires_auth_and_rejects_extra_fields() -> None:
    service = _service()
    with TestClient(_app(service)) as client:
        unauthenticated = client.patch(
            "/api/v1/auth/me",
            json={"display_name": "New name"},
        )
        _register(client)
        updated = client.patch(
            "/api/v1/auth/me",
            json={"display_name": "  Updated name  "},
        )
        cleared = client.patch(
            "/api/v1/auth/me",
            json={"display_name": ""},
        )
        invalid = client.patch(
            "/api/v1/auth/me",
            json={"display_name": "bad\nname"},
        )
        extra = client.patch(
            "/api/v1/auth/me",
            json={"display_name": "Name", "role": "admin"},
        )
        disallowed_origin = client.patch(
            "/api/v1/auth/me",
            json={"display_name": "Name"},
            headers={"Origin": "https://attacker.example"},
        )

    assert unauthenticated.status_code == 401
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Updated name"
    assert updated.headers["cache-control"] == "no-store"
    assert "password_hash" not in updated.json()
    assert cleared.status_code == 200
    assert cleared.json()["display_name"] is None
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "display_name_invalid"
    assert extra.status_code == 422
    assert disallowed_origin.status_code == 403


def test_admin_dependency_returns_401_403_and_allows_current_admin() -> None:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    current_user = User(
        id="usr_admin_check",
        email="admin-check@example.com",
        password_hash="not-exposed",
        display_name=None,
        status=UserStatus.ACTIVE,
        created_at=timestamp,
        updated_at=timestamp,
    )
    context = AuthContext(user=current_user, session_id="ses_admin_check")
    auth_state = {"context": context}

    class AuthenticatorStub:
        async def authenticate(self, token: str) -> AuthContext | None:
            return auth_state["context"] if token == "valid" else None

    app = FastAPI()
    app.state.authenticator = AuthenticatorStub()
    app.state.auth_cookie_name = "athena_session"

    @app.get("/admin-check")
    async def admin_check(_: AdminAuth) -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        unauthenticated = client.get("/admin-check")
        client.cookies.set("athena_session", "valid")
        non_admin = client.get("/admin-check")
        auth_state["context"] = replace(
            context,
            user=replace(context.user, role=UserRole.ADMIN),
        )
        admin = client.get("/admin-check")

    assert unauthenticated.status_code == 401
    assert non_admin.status_code == 403
    assert admin.status_code == 200


def test_cookie_can_be_secure_and_remember_me_extends_max_age() -> None:
    service = _service()
    with TestClient(_app(service, secure=True)) as client:
        register = client.post(
            "/api/v1/auth/register",
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
            },
        )
        login = client.post(
            "/api/v1/auth/login",
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
                "remember_me": True,
            },
        )

    assert "secure" in register.headers["set-cookie"].lower()
    assert f"max-age={REMEMBER_SESSION_SECONDS}" in login.headers["set-cookie"].lower()
    assert register.json()["role"] == "user"
    assert login.json()["role"] == "user"


def test_login_errors_and_logout_are_stable() -> None:
    service = _service()
    with TestClient(_app(service)) as client:
        unknown = client.post(
            "/api/v1/auth/login",
            json={
                "email": "missing@example.com",
                "password": "long-enough-password",
            },
        )
        _register(client)
        wrong = client.post(
            "/api/v1/auth/login",
            json={
                "email": "user@example.com",
                "password": "wrong-password-value",
            },
        )
        logged_in = client.post(
            "/api/v1/auth/login",
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
            },
        )
        logout = client.post("/api/v1/auth/logout")
        after_logout = client.get("/api/v1/auth/me")

    assert unknown.status_code == 401
    assert unknown.json() == {"detail": "invalid_credentials"}
    assert wrong.status_code == 401
    assert wrong.json() == {"detail": "invalid_credentials"}
    assert logged_in.status_code == 200
    assert logout.status_code == 204
    assert "max-age=0" in logout.headers["set-cookie"].lower()
    assert after_logout.status_code == 401
    assert after_logout.json() == {"detail": "not_authenticated"}


def test_register_validation_duplicate_and_extra_fields() -> None:
    service = _service()
    with TestClient(_app(service)) as client:
        invalid_email = client.post(
            "/api/v1/auth/register",
            json={"email": "invalid", "password": "long-enough-password"},
        )
        short_password = client.post(
            "/api/v1/auth/register",
            json={"email": "user@example.com", "password": "short"},
        )
        extra = client.post(
            "/api/v1/auth/register",
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
                "role": "admin",
            },
        )
        _register(client)
        duplicate = client.post(
            "/api/v1/auth/register",
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
            },
        )

    assert invalid_email.status_code == 422
    assert invalid_email.json() == {"detail": "invalid_email"}
    assert short_password.status_code == 422
    assert short_password.json() == {"detail": "password_invalid"}
    assert extra.status_code == 422
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "email_exists"}


def test_password_change_keeps_current_session_and_revokes_others() -> None:
    service = _service()
    app = _app(service)
    with TestClient(app) as first_client, TestClient(app) as second_client:
        _register(first_client)
        login = second_client.post(
            "/api/v1/auth/login",
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
            },
        )
        changed = first_client.post(
            "/api/v1/auth/password",
            json={
                "current_password": "long-enough-password",
                "new_password": "new-long-enough-password",
            },
        )
        first_me = first_client.get("/api/v1/auth/me")
        second_me = second_client.get("/api/v1/auth/me")
        old_login = second_client.post(
            "/api/v1/auth/login",
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
            },
        )
        new_login = second_client.post(
            "/api/v1/auth/login",
            json={
                "email": "user@example.com",
                "password": "new-long-enough-password",
            },
        )

    assert login.status_code == 200
    assert changed.status_code == 204
    assert first_me.status_code == 200
    assert second_me.status_code == 401
    assert old_login.status_code == 401
    assert new_login.status_code == 200


def test_origin_guard_and_login_rate_limit() -> None:
    service = _service(login_limit=1)
    with TestClient(_app(service, public_origin="https://app.example.com")) as client:
        rejected = client.post(
            "/api/v1/auth/register",
            headers={"Origin": "https://evil.example.com"},
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
            },
        )
        accepted = client.post(
            "/api/v1/auth/register",
            headers={"Origin": "https://app.example.com"},
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
            },
        )
        first_failure = client.post(
            "/api/v1/auth/login",
            json={
                "email": "user@example.com",
                "password": "wrong-password-value",
            },
        )
        limited = client.post(
            "/api/v1/auth/login",
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
            },
        )

    assert rejected.status_code == 403
    assert rejected.json() == {"detail": "origin_not_allowed"}
    assert accepted.status_code == 201
    assert first_failure.status_code == 401
    assert limited.status_code == 429
    assert limited.json() == {"detail": "rate_limited"}


def test_me_requires_authentication_and_password_requires_current_password() -> None:
    service = _service()
    with TestClient(_app(service)) as client:
        unauthenticated = client.get("/api/v1/auth/me")
        _register(client)
        wrong_current = client.post(
            "/api/v1/auth/password",
            json={
                "current_password": "wrong-current-password",
                "new_password": "new-long-enough-password",
            },
        )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"detail": "not_authenticated"}
    assert wrong_current.status_code == 400
    assert wrong_current.json() == {"detail": "current_password_invalid"}


def test_change_password_api_returns_429_after_repeated_failures() -> None:
    """改密接口连续 5 次当前密码错误后，第 6 次请求返回 429 rate_limited。"""
    service = _service()
    with TestClient(_app(service)) as client:
        _register(client)
        for _ in range(5):
            resp = client.post(
                "/api/v1/auth/password",
                json={
                    "current_password": "wrong-current-password",
                    "new_password": "new-long-enough-password",
                },
            )
            assert resp.status_code == 400
            assert resp.json() == {"detail": "current_password_invalid"}
        limited = client.post(
            "/api/v1/auth/password",
            json={
                "current_password": "long-enough-password",
                "new_password": "new-long-enough-password",
            },
        )

    assert limited.status_code == 429
    assert limited.json() == {"detail": "rate_limited"}


def test_auth_endpoints_return_503_when_rate_limiter_is_unavailable() -> None:
    """限流后端故障时，注册、登录与改密接口均 fail-safe 返回 503 rate_limit_unavailable。"""
    from app.auth.rate_limit import RateLimitUnavailableError

    class _UnavailableLimiter:
        async def consume(self, key: str) -> bool:
            del key
            raise RateLimitUnavailableError("rate_limit_unavailable")

        async def reset(self, key: str) -> None:
            del key
            raise RateLimitUnavailableError("rate_limit_unavailable")

    unavailable = _UnavailableLimiter()

    # 1. 注册和登录在限流器不可用时直接返回 503。
    failing_service = AuthService(
        InMemoryAuthRepository(),
        FakePasswordHasher(),
        user_id_factory=_incrementing_factory("usr_"),
        auth_session_id_factory=_incrementing_factory("ses_"),
        token_factory=_incrementing_factory("token_"),
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        login_account_limiter=unavailable,
        login_ip_limiter=unavailable,
        register_limiter=unavailable,
        change_password_limiter=unavailable,
    )
    with TestClient(_app(failing_service)) as client:
        register_resp = client.post(
            "/api/v1/auth/register",
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
                "display_name": "Athena",
            },
        )
        login_resp = client.post(
            "/api/v1/auth/login",
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
            },
        )

    assert register_resp.status_code == 503
    assert register_resp.json() == {"detail": "rate_limit_unavailable"}
    assert login_resp.status_code == 503
    assert login_resp.json() == {"detail": "rate_limit_unavailable"}

    # 2. 已登录用户改密在限流器不可用时同样返回 503。
    change_password_service = AuthService(
        InMemoryAuthRepository(),
        FakePasswordHasher(),
        user_id_factory=_incrementing_factory("usr_"),
        auth_session_id_factory=_incrementing_factory("ses_"),
        token_factory=_incrementing_factory("token_"),
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        change_password_limiter=unavailable,
    )
    with TestClient(_app(change_password_service)) as client:
        _register(client)
        password_resp = client.post(
            "/api/v1/auth/password",
            json={
                "current_password": "long-enough-password",
                "new_password": "new-long-enough-password",
            },
        )

    assert password_resp.status_code == 503
    assert password_resp.json() == {"detail": "rate_limit_unavailable"}
