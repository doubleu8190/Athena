"""认证 HTTP API 测试。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import AuthCookieConfig, build_auth_router
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
    assert "password" not in response.json()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert f"max-age={DEFAULT_SESSION_SECONDS}" in cookie
    assert response.headers["cache-control"] == "no-store"
    assert me.status_code == 200
    assert me.json()["id"] == response.json()["id"]


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
