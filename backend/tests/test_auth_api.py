"""认证 HTTP API 测试（本地应急登录、关闭开放注册、提供方声明与权限检查）。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from auth_support import (
    FakePasswordHasher,
    InMemoryAuthRepository,
    provision_local_user_sync,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import AuthCookieConfig, build_auth_router
from api.auth_middleware import AuthPrecheckMiddleware
from api.security import AdminAuth
from app.auth.rate_limit import FixedWindowRateLimiter
from app.auth.service import (
    DEFAULT_SESSION_SECONDS,
    REMEMBER_SESSION_SECONDS,
    AuthService,
)
from domain.auth import (
    AuthContext,
    AuthProvider,
    User,
    UserRole,
    UserStatus,
)


def _incrementing_factory(prefix: str) -> Callable[[], str]:
    counter = 0

    def create() -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}{counter}"

    return create


def _service(
    *,
    login_limit: int = 5,
    repository: InMemoryAuthRepository | None = None,
) -> tuple[AuthService, InMemoryAuthRepository]:
    repo = repository or InMemoryAuthRepository()
    service = AuthService(
        repo,
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
    return service, repo


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
    app.add_middleware(AuthPrecheckMiddleware, fastapi_app=app)
    app.include_router(
        build_auth_router(
            service,
            cookie=AuthCookieConfig(secure=secure),
        )
    )
    return app


def _provision_and_login(
    service: AuthService,
    client: TestClient,
    *,
    email: str = "user@example.com",
    password: str = "long-enough-password",
    display_name: str | None = "Athena",
) -> None:
    provision_local_user_sync(
        service,
        email=email,
        password=password,
        display_name=display_name,
    )
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200


def test_public_register_route_is_removed_and_providers_endpoint_works() -> None:
    """公开自助注册接口必须彻底下线（未登录拦 401、已登录 404），且 /providers 准确声明认证方式。"""
    service, _ = _service()
    with TestClient(_app(service)) as client:
        unauth_register_resp = client.post(
            "/api/v1/auth/register",
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
                "display_name": "Athena",
            },
        )
        providers_resp = client.get("/api/v1/auth/providers")
        mock_users_resp = client.get("/api/v1/auth/mock/users")
        mock_login_resp = client.post(
            "/api/v1/auth/mock/login", json={"user_id": "mock_super_admin"}
        )
        _provision_and_login(service, client)
        authed_register_resp = client.post(
            "/api/v1/auth/register",
            json={
                "email": "user2@example.com",
                "password": "long-enough-password",
                "display_name": "Athena 2",
            },
        )

    assert unauth_register_resp.status_code == 401
    assert authed_register_resp.status_code == 404
    assert providers_resp.status_code == 200
    assert providers_resp.headers["cache-control"] == "no-store"
    assert providers_resp.json() == {
        "feishu_enabled": False,
        "local_login_enabled": True,
        "mock_enabled": False,
    }
    assert mock_users_resp.status_code == 404
    assert mock_login_resp.status_code == 404


def test_login_sets_http_only_cookie_and_me_returns_extended_safe_user() -> None:
    service, _ = _service()
    provision_local_user_sync(
        service,
        email=" User@Example.COM ",
        password="long-enough-password",
        display_name="Athena",
    )
    with TestClient(_app(service)) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={
                "email": " User@Example.COM ",
                "password": "long-enough-password",
            },
        )
        cookie = response.headers["set-cookie"].lower()
        me = client.get("/api/v1/auth/me")

    assert response.status_code == 200
    assert response.json()["email"] == "user@example.com"
    assert response.json()["display_name"] == "Athena"
    assert response.json()["role"] == "member"
    assert response.json()["auth_provider"] == "local"
    assert response.json()["departments"] == []
    assert response.json()["managed_department_ids"] == []
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
    assert me.json()["role"] == "member"
    assert me.json()["auth_provider"] == "local"


def test_update_current_display_name_requires_auth_and_rejects_extra_fields() -> None:
    service, _ = _service()
    with TestClient(_app(service)) as client:
        unauthenticated = client.patch(
            "/api/v1/auth/me",
            json={"display_name": "New name"},
        )
        _provision_and_login(service, client)
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
            json={"display_name": "Name", "role": "super_admin"},
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


def test_admin_dependency_returns_401_403_and_allows_super_admin_only() -> None:
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
        member = client.get("/admin-check")
        auth_state["context"] = replace(
            context,
            user=replace(context.user, role=UserRole.DEPT_ADMIN),
        )
        dept_admin = client.get("/admin-check")
        auth_state["context"] = replace(
            context,
            user=replace(context.user, role=UserRole.SUPER_ADMIN),
        )
        super_admin = client.get("/admin-check")

    assert unauthenticated.status_code == 401
    assert member.status_code == 403
    assert dept_admin.status_code == 403
    assert super_admin.status_code == 200


def test_cookie_can_be_secure_and_remember_me_extends_max_age() -> None:
    service, _ = _service()
    provision_local_user_sync(service, email="user@example.com", password="long-enough-password")
    with TestClient(_app(service, secure=True)) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
                "remember_me": True,
            },
        )

    assert "secure" in login.headers["set-cookie"].lower()
    assert f"max-age={REMEMBER_SESSION_SECONDS}" in login.headers["set-cookie"].lower()
    assert login.json()["role"] == "member"


def test_login_errors_and_logout_are_stable() -> None:
    service, _ = _service()
    with TestClient(_app(service)) as client:
        unknown = client.post(
            "/api/v1/auth/login",
            json={
                "email": "missing@example.com",
                "password": "long-enough-password",
            },
        )
        provision_local_user_sync(
            service,
            email="user@example.com",
            password="long-enough-password",
        )
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


def test_non_local_account_rejected_by_password_login_and_password_change() -> None:
    """飞书与 Mock 账号不得通过邮箱密码登录，也不支持 /auth/password 改密。"""
    service, repo = _service()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    feishu_user = User(
        id="usr_feishu_1",
        email="feishu@example.com",
        password_hash=None,
        display_name="飞书员工",
        status=UserStatus.ACTIVE,
        role=UserRole.MEMBER,
        auth_provider=AuthProvider.FEISHU,
        external_union_id="on_u1",
        external_open_id="ou_o1",
        created_at=now,
        updated_at=now,
    )
    repo.users[feishu_user.id] = feishu_user
    # 手动签发一个有效会话用于测试 /auth/password 返回 400 password_change_not_supported。
    auth_session, token, _ = service._new_session(feishu_user.id, now, DEFAULT_SESSION_SECONDS)
    asyncio.run(repo.create_session(auth_session))

    with TestClient(_app(service)) as client:
        login_resp = client.post(
            "/api/v1/auth/login",
            json={"email": "feishu@example.com", "password": "long-enough-password"},
        )
        client.cookies.set("athena_session", token)
        change_pwd_resp = client.post(
            "/api/v1/auth/password",
            json={
                "current_password": "long-enough-password",
                "new_password": "new-long-enough-password",
            },
        )

    assert login_resp.status_code == 401
    assert login_resp.json() == {"detail": "invalid_credentials"}
    assert change_pwd_resp.status_code == 400
    assert change_pwd_resp.json() == {"detail": "password_change_not_supported"}


def test_password_change_keeps_current_session_and_revokes_others() -> None:
    service, _ = _service()
    provision_local_user_sync(service, email="user@example.com", password="long-enough-password")
    app = _app(service)
    with TestClient(app) as first_client, TestClient(app) as second_client:
        first_login = first_client.post(
            "/api/v1/auth/login",
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
            },
        )
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

    assert first_login.status_code == 200
    assert login.status_code == 200
    assert changed.status_code == 204
    assert first_me.status_code == 200
    assert second_me.status_code == 401
    assert old_login.status_code == 401
    assert new_login.status_code == 200


def test_origin_guard_and_login_rate_limit() -> None:
    service, _ = _service(login_limit=1)
    provision_local_user_sync(service, email="user@example.com", password="long-enough-password")
    with TestClient(_app(service, public_origin="https://app.example.com")) as client:
        rejected = client.post(
            "/api/v1/auth/login",
            headers={"Origin": "https://evil.example.com"},
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
            },
        )
        first_failure = client.post(
            "/api/v1/auth/login",
            headers={"Origin": "https://app.example.com"},
            json={
                "email": "user@example.com",
                "password": "wrong-password-value",
            },
        )
        limited = client.post(
            "/api/v1/auth/login",
            headers={"Origin": "https://app.example.com"},
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
            },
        )

    assert rejected.status_code == 403
    assert rejected.json() == {"detail": "origin_not_allowed"}
    assert first_failure.status_code == 401
    assert limited.status_code == 429
    assert limited.json() == {"detail": "rate_limited"}


def test_me_requires_authentication_and_password_requires_current_password() -> None:
    service, _ = _service()
    with TestClient(_app(service)) as client:
        unauthenticated = client.get("/api/v1/auth/me")
        _provision_and_login(service, client)
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
    service, _ = _service()
    with TestClient(_app(service)) as client:
        _provision_and_login(service, client)
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
    """限流后端故障时，登录与改密接口均 fail-safe 返回 503 rate_limit_unavailable。"""
    from app.auth.rate_limit import RateLimitUnavailableError

    class _UnavailableLimiter:
        async def consume(self, key: str) -> bool:
            del key
            raise RateLimitUnavailableError("rate_limit_unavailable")

        async def reset(self, key: str) -> None:
            del key
            raise RateLimitUnavailableError("rate_limit_unavailable")

    unavailable = _UnavailableLimiter()

    # 1. 登录在限流器不可用时直接返回 503。
    failing_service = AuthService(
        InMemoryAuthRepository(),
        FakePasswordHasher(),
        user_id_factory=_incrementing_factory("usr_"),
        auth_session_id_factory=_incrementing_factory("ses_"),
        token_factory=_incrementing_factory("token_"),
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        login_account_limiter=unavailable,
        login_ip_limiter=unavailable,
        change_password_limiter=unavailable,
    )
    with TestClient(_app(failing_service)) as client:
        login_resp = client.post(
            "/api/v1/auth/login",
            json={
                "email": "user@example.com",
                "password": "long-enough-password",
            },
        )

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
        _provision_and_login(change_password_service, client)
        password_resp = client.post(
            "/api/v1/auth/password",
            json={
                "current_password": "long-enough-password",
                "new_password": "new-long-enough-password",
            },
        )

    assert password_resp.status_code == 503
    assert password_resp.json() == {"detail": "rate_limit_unavailable"}
