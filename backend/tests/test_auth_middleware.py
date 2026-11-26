"""全局前置同源校验与登录态鉴权中间件回归测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest
from auth_support import TEST_PASSWORD, make_auth_service, provision_local_user_sync
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from api.auth_middleware import AuthPrecheckMiddleware
from api.security import AdminAuth, CurrentAuth, get_current_auth, require_admin
from api.upload_middleware import UploadAdmissionMiddleware
from domain.auth import AuthContext, Authenticator, User, UserRole, UserStatus
from main import create_app

NOW = datetime(2026, 11, 23, tzinfo=UTC)


def _make_context(user_id: str, role: UserRole) -> AuthContext:
    return AuthContext(
        user=User(
            id=user_id,
            email=f"{user_id}@example.test",
            password_hash="hash",
            display_name=None,
            status=UserStatus.ACTIVE,
            role=role,
            created_at=NOW,
            updated_at=NOW,
        ),
        session_id=f"ses_{user_id}",
    )


class _CountingAuthenticator:
    """记录每次鉴权调用并支持模拟用户、管理员与数据库异常。"""

    def __init__(self) -> None:
        self.seen_tokens: list[str] = []

    async def authenticate(self, token: str) -> AuthContext | None:
        self.seen_tokens.append(token)
        if token == "db_sql_error":
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        if token == "db_os_error":
            raise OSError("socket reset")
        if token == "member_token":
            return _make_context("usr_member", UserRole.MEMBER)
        if token == "admin_token":
            return _make_context("usr_admin", UserRole.SUPER_ADMIN)
        return None


def test_public_whitelist_and_health_and_options_pass_through() -> None:
    """健康检查、OPTIONS 预检与公开认证写接口无需 Cookie 直接放行。"""

    async def scenario() -> None:
        auth = _CountingAuthenticator()
        reached: list[tuple[str, str]] = []

        async def downstream(scope: dict[str, object], receive: object, send: object) -> None:
            del receive
            assert callable(send)
            reached.append((str(scope["method"]), str(scope["path"])))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = AuthPrecheckMiddleware(downstream, authenticator=auth)

        async def noop_receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        allowed_cases = (
            ("GET", "/health"),
            ("OPTIONS", "/api/v1/projects"),
            ("GET", "/api/v1/auth/providers"),
            ("POST", "/api/v1/auth/login"),
            ("POST", "/api/v1/auth/logout"),
            ("GET", "/api/v1/auth/feishu/authorize"),
            ("GET", "/api/v1/auth/feishu/callback"),
            ("GET", "/api/v1/auth/mock/users"),
            ("POST", "/api/v1/auth/mock/login"),
        )
        for method, path in allowed_cases:
            responses: list[dict[str, object]] = []

            async def send(
                message: dict[str, object],
                sink: list[dict[str, object]] = responses,
            ) -> None:
                sink.append(message)

            await middleware(
                {"type": "http", "method": method, "path": path, "headers": []},
                noop_receive,
                send,
            )
            assert responses[0]["status"] == 200

        assert reached == list(allowed_cases)
        assert auth.seen_tokens == []

        # 非 POST 方法访问公开认证路径不命中白名单，未登录应被 401 拦截。
        rejected_responses: list[dict[str, object]] = []

        async def send_rejected(message: dict[str, object]) -> None:
            rejected_responses.append(message)

        await middleware(
            {"type": "http", "method": "GET", "path": "/api/v1/auth/login", "headers": []},
            noop_receive,
            send_rejected,
        )
        assert rejected_responses[0]["status"] == 401
        assert rejected_responses[-1]["body"] == b'{"detail":"not_authenticated"}'

    asyncio.run(scenario())


def test_protected_routes_reject_unauthenticated_before_downstream_and_slot() -> None:
    """未登录或无效 Cookie 在触碰下游路由、上传槽位和读体之前返回 401。"""

    async def scenario() -> None:
        auth = _CountingAuthenticator()
        app_calls = 0

        async def downstream(scope: object, receive: object, send: object) -> None:
            nonlocal app_calls
            del scope, receive, send
            app_calls += 1

        upload_layer = UploadAdmissionMiddleware(downstream)
        auth_layer = AuthPrecheckMiddleware(upload_layer, authenticator=auth)

        async def fail_receive() -> dict[str, object]:
            raise AssertionError("request body must not be read for unauthenticated request")

        for headers in (
            [],
            [(b"cookie", b"athena_session=expired_or_invalid")],
        ):
            responses: list[dict[str, object]] = []

            async def send(
                message: dict[str, object],
                sink: list[dict[str, object]] = responses,
            ) -> None:
                sink.append(message)

            await auth_layer(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/v1/knowledge-bases/kb_1/documents/upload",
                    "headers": headers,
                },
                fail_receive,
                send,
            )
            assert responses[0]["status"] == 401
            assert responses[-1]["body"] == b'{"detail":"not_authenticated"}'

        assert app_calls == 0
        assert upload_layer._local_active["knowledge"] == 0
        assert auth.seen_tokens == ["expired_or_invalid"]

    asyncio.run(scenario())


def test_admin_routes_enforce_admin_role_and_malformed_cookie_tolerance() -> None:
    """/api/v1/admin/* 路径普通用户返回 403，管理员放行，且容忍前置畸形 Cookie 段。"""

    async def scenario() -> None:
        auth = _CountingAuthenticator()
        app_calls = 0

        async def downstream(scope: object, receive: object, send: object) -> None:
            nonlocal app_calls
            del scope, receive
            assert callable(send)
            app_calls += 1
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = AuthPrecheckMiddleware(downstream, authenticator=auth)

        async def fail_receive() -> dict[str, object]:
            raise AssertionError("body must not be read when rejected")

        # 1. 普通用户携带前置畸形 Cookie 段访问管理员路径 -> 403 admin_required
        member_responses: list[dict[str, object]] = []

        async def send_member(message: dict[str, object]) -> None:
            member_responses.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/admin/skills/import",
                "headers": [(b"cookie", b"bad[cookie]=1; athena_session=member_token")],
            },
            fail_receive,
            send_member,
        )
        assert member_responses[0]["status"] == 403
        assert member_responses[-1]["body"] == b'{"detail":"admin_required"}'
        assert app_calls == 0

        # 2. 管理员凭据访问 /api/v1/admin/tools -> 放行
        admin_responses: list[dict[str, object]] = []

        async def send_admin(message: dict[str, object]) -> None:
            admin_responses.append(message)

        await middleware(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/admin/tools",
                "headers": [(b"cookie", b"athena_session=admin_token")],
            },
            fail_receive,
            send_admin,
        )
        assert admin_responses[0]["status"] == 200
        assert app_calls == 1

    asyncio.run(scenario())


def test_cross_origin_write_requests_return_403_before_database_lookup() -> None:
    """跨站 Origin 写请求在查询会话前直接返回 403，且允许匹配同源或 public_origin。"""

    async def scenario() -> None:
        auth = _CountingAuthenticator()
        reached: list[str] = []

        async def downstream(scope: dict[str, object], receive: object, send: object) -> None:
            del receive
            assert callable(send)
            reached.append(str(scope["path"]))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = AuthPrecheckMiddleware(
            downstream,
            authenticator=auth,
            public_origin="https://app.example.com",
        )

        async def noop_receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        # 1. 受保护写接口携带跨站 Origin：直接 403，绝不触发 authenticate 查库
        for path in ("/api/v1/projects", "/api/v1/auth/login", "/api/v1/auth/logout"):
            responses: list[dict[str, object]] = []

            async def send(
                message: dict[str, object],
                sink: list[dict[str, object]] = responses,
            ) -> None:
                sink.append(message)

            await middleware(
                {
                    "type": "http",
                    "scheme": "https",
                    "method": "POST",
                    "path": path,
                    "headers": [
                        (b"host", b"api.example.com"),
                        (b"origin", b"https://evil.example.com"),
                        (b"cookie", b"athena_session=member_token"),
                    ],
                },
                noop_receive,
                send,
            )
            assert responses[0]["status"] == 403
            assert responses[-1]["body"] == b'{"detail":"origin_not_allowed"}'

        assert auth.seen_tokens == []
        assert reached == []

        # 2. 携带与 public_origin 匹配的 Origin -> 通过 Origin 检查并完成鉴权放行
        ok_responses: list[dict[str, object]] = []

        async def send_ok(message: dict[str, object]) -> None:
            ok_responses.append(message)

        await middleware(
            {
                "type": "http",
                "scheme": "https",
                "method": "POST",
                "path": "/api/v1/projects",
                "headers": [
                    (b"host", b"api.example.com"),
                    (b"origin", b"https://app.example.com"),
                    (b"cookie", b"athena_session=member_token"),
                ],
            },
            noop_receive,
            send_ok,
        )
        assert ok_responses[0]["status"] == 200
        assert reached == ["/api/v1/projects"]
        assert auth.seen_tokens == ["member_token"]

        # 3. GET 请求携带任意 Origin 不受写请求 Origin 限制（但仍走鉴权）
        get_responses: list[dict[str, object]] = []

        async def send_get(message: dict[str, object]) -> None:
            get_responses.append(message)

        await middleware(
            {
                "type": "http",
                "scheme": "https",
                "method": "GET",
                "path": "/api/v1/projects",
                "headers": [
                    (b"host", b"api.example.com"),
                    (b"origin", b"https://evil.example.com"),
                    (b"cookie", b"athena_session=member_token"),
                ],
            },
            noop_receive,
            send_get,
        )
        assert get_responses[0]["status"] == 200

    asyncio.run(scenario())


@pytest.mark.parametrize("token", ("db_sql_error", "db_os_error"))
def test_auth_database_errors_map_to_503_database_unavailable(token: str) -> None:
    """鉴权期遭遇 SQLAlchemyError 或 OSError 时稳定返回 503 database_unavailable。"""

    async def scenario() -> None:
        auth = _CountingAuthenticator()
        app_calls = 0

        async def downstream(scope: object, receive: object, send: object) -> None:
            nonlocal app_calls
            del scope, receive, send
            app_calls += 1

        middleware = AuthPrecheckMiddleware(downstream, authenticator=auth)
        responses: list[dict[str, object]] = []

        async def noop_receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            responses.append(message)

        await middleware(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/projects",
                "headers": [(b"cookie", f"athena_session={token}".encode("ascii"))],
            },
            noop_receive,
            send,
        )
        assert app_calls == 0
        assert responses[0]["status"] == 503
        assert responses[-1]["body"] == b'{"detail":"database_unavailable"}'

    asyncio.run(scenario())


def test_single_request_reuses_pre_auth_context_and_calls_authenticator_once() -> None:
    """单次请求经过 AuthPrecheckMiddleware 与 CurrentAuth/AdminAuth 路由仅查库 1 次。"""
    app = FastAPI()
    auth = _CountingAuthenticator()
    app.state.authenticator = auth
    app.state.auth_cookie_name = "athena_session"
    app.add_middleware(AuthPrecheckMiddleware, fastapi_app=app)

    @app.get("/api/v1/profile")
    async def profile(current_auth: CurrentAuth) -> dict[str, str]:
        return {"user_id": current_auth.user.id}

    @app.get("/api/v1/admin/panel")
    async def admin_panel(admin_auth: AdminAuth) -> dict[str, str]:
        return {"admin_id": admin_auth.user.id}

    with TestClient(app) as client:
        # 1. 普通用户请求 CurrentAuth 路由（带前置畸形 Cookie）-> 仅调 authenticate 1 次
        resp_user = client.get(
            "/api/v1/profile",
            headers={"Cookie": "bad[cookie]=1; athena_session=member_token"},
        )
        assert resp_user.status_code == 200
        assert resp_user.json() == {"user_id": "usr_member"}
        assert auth.seen_tokens == ["member_token"]

        # 2. 管理员请求 AdminAuth 路由 -> 仅调 authenticate 1 次（累计 2 次）
        resp_admin = client.get(
            "/api/v1/admin/panel",
            headers={"Cookie": "athena_session=admin_token"},
        )
        assert resp_admin.status_code == 200
        assert resp_admin.json() == {"admin_id": "usr_admin"}
        assert auth.seen_tokens == ["member_token", "admin_token"]

        # 3. 动态替换 app.state.auth_cookie_name 与 app.state.authenticator 立即生效
        second_auth = _CountingAuthenticator()
        app.state.authenticator = cast(Authenticator, second_auth)
        app.state.auth_cookie_name = "custom_sid"

        resp_custom = client.get(
            "/api/v1/profile",
            headers={"Cookie": "custom_sid=member_token"},
        )
        assert resp_custom.status_code == 200
        assert resp_custom.json() == {"user_id": "usr_member"}
        assert second_auth.seen_tokens == ["member_token"]
        assert len(auth.seen_tokens) == 2


def test_dependency_overrides_bypass_precheck_while_keeping_origin_check() -> None:
    """FastAPI dependency_overrides 覆盖鉴权依赖时中间件放行，但不放松跨站 Origin 校验。"""
    app = FastAPI()
    auth = _CountingAuthenticator()
    app.state.authenticator = auth
    app.state.auth_cookie_name = "athena_session"
    app.add_middleware(AuthPrecheckMiddleware, fastapi_app=app)

    @app.post("/api/v1/projects")
    async def create_project(current_auth: CurrentAuth) -> dict[str, str]:
        return {"owner": current_auth.user.id}

    @app.get("/api/v1/admin/items")
    async def admin_items(admin_auth: AdminAuth) -> dict[str, str]:
        return {"admin": admin_auth.user.id}

    app.dependency_overrides[get_current_auth] = lambda: _make_context(
        "usr_override", UserRole.MEMBER
    )
    app.dependency_overrides[require_admin] = lambda: _make_context(
        "usr_boss", UserRole.SUPER_ADMIN
    )

    with TestClient(app) as client:
        # 1. 无 Cookie 时依赖覆盖生效
        ok_project = client.post("/api/v1/projects")
        assert ok_project.status_code == 200
        assert ok_project.json() == {"owner": "usr_override"}

        ok_admin = client.get("/api/v1/admin/items")
        assert ok_admin.status_code == 200
        assert ok_admin.json() == {"admin": "usr_boss"}

        # 2. 跨站 Origin 仍被 403 拦截
        cross_origin = client.post(
            "/api/v1/projects",
            headers={"Origin": "https://evil.example.com"},
        )
        assert cross_origin.status_code == 403
        assert cross_origin.json() == {"detail": "origin_not_allowed"}

        # 3. 清除覆盖后恢复中间件前置拦截
        app.dependency_overrides.clear()
        unauth = client.post("/api/v1/projects")
        assert unauth.status_code == 401
        assert unauth.json() == {"detail": "not_authenticated"}

        assert auth.seen_tokens == []


def test_create_app_runs_auth_precheck_before_upload_admission() -> None:
    """create_app 中外层 AuthPrecheckMiddleware 先于内层 UploadAdmissionMiddleware 执行。"""
    auth_service = make_auth_service()
    provision_local_user_sync(auth_service, email="member@example.com", password=TEST_PASSWORD)
    app = create_app(auth_service=auth_service)

    with TestClient(app) as client:
        # 1. 未登录上传即使携带超限 Content-Length，也必须先被外层鉴权拦截为 401（而非内层 413）
        unauth_upload = client.post(
            "/api/v1/knowledge-bases/kb_1/documents/upload",
            headers={
                "Content-Type": "multipart/form-data; boundary=x",
                "Content-Length": str(50 * 1024 * 1024),
            },
        )
        assert unauth_upload.status_code == 401
        assert unauth_upload.json() == {"detail": "not_authenticated"}

        # 2. 登录普通成员账号
        logged_in = client.post(
            "/api/v1/auth/login",
            json={"email": "member@example.com", "password": TEST_PASSWORD},
        )
        assert logged_in.status_code == 200

        # 3. 普通用户请求管理员技能上传，即使携带超限 Content-Length 也先被外层角色预检为 403
        non_admin_upload = client.post(
            "/api/v1/admin/skills/import",
            headers={
                "Content-Type": "multipart/form-data; boundary=x",
                "Content-Length": str(50 * 1024 * 1024),
            },
        )
        assert non_admin_upload.status_code == 403
        assert non_admin_upload.json() == {"detail": "admin_required"}

        # 4. 普通用户请求个人 Markdown 上传且携带超限 Content-Length -> 通过外层鉴权后由内层返回 413
        oversized_personal = client.post(
            "/api/v1/me/skills/parse",
            headers={
                "Content-Type": "multipart/form-data; boundary=x",
                "Content-Length": str(5 * 1024 * 1024),
            },
        )
        assert oversized_personal.status_code == 413
        assert oversized_personal.json() == {"detail": "skill_markdown_too_large"}


@pytest.mark.parametrize(
    ("headers", "expected_seen_tokens"),
    (
        (
            [(b"cookie", b"athena_session=member_token; athena_session=")],
            [],
        ),
        (
            [(b"cookie", b"athena_session=member_token; athena_session=bad value")],
            ["bad value"],
        ),
        (
            [
                (b"cookie", b"athena_session=member_token"),
                (b"cookie", b"athena_session="),
            ],
            [],
        ),
    ),
)
def test_duplicate_cookie_override_matches_starlette_and_blocks_at_precheck(
    headers: list[tuple[bytes, bytes]],
    expected_seen_tokens: list[str],
) -> None:
    """同名 Cookie 尾段为空或非法字符覆盖前段时，与 Starlette 口径一致并在外层直接拦截 401。"""

    async def scenario() -> None:
        auth = _CountingAuthenticator()
        downstream_calls = 0

        async def downstream(scope: object, receive: object, send: object) -> None:
            nonlocal downstream_calls
            del scope, receive, send
            downstream_calls += 1

        middleware = AuthPrecheckMiddleware(downstream, authenticator=auth)
        responses: list[dict[str, object]] = []

        async def fail_receive() -> dict[str, object]:
            raise AssertionError("body must not be read when overridden cookie is invalid")

        async def send(message: dict[str, object]) -> None:
            responses.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/knowledge-bases/kb_1/documents/upload",
                "headers": headers,
            },
            fail_receive,
            send,
        )
        assert downstream_calls == 0
        assert responses[0]["status"] == 401
        assert responses[-1]["body"] == b'{"detail":"not_authenticated"}'
        assert auth.seen_tokens == expected_seen_tokens

    asyncio.run(scenario())
