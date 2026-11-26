"""飞书 HTTP 客户端、OAuth 一次性 state、飞书原生 SSO 回调与通讯录组织树同步单元测试。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from cli.bootstrap_super_admin import bootstrap_super_admin
from cli.sync_feishu_org import run_feishu_org_sync
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.models import Base
from adapter.org import DepartmentModel, SqlAlchemyOrgRepository, UserDepartmentModel
from api.auth import AuthCookieConfig, build_auth_router
from app.auth import (
    AuthService,
    InMemoryOAuthStateStore,
    RedisOAuthStateStore,
    validate_safe_return_to,
)
from app.org import (
    FeishuOrgSyncService,
    OrgService,
    seed_mock_organization,
    validate_and_order_departments,
)
from app.org.feishu_sync import _merge_remote_employees
from bootstrap.api import build_api_dependencies
from domain.auth import (
    AuthProvider,
    InvalidReturnToError,
    OAuthStateUnavailableError,
    UserRole,
    UserStatus,
)
from domain.org import (
    DepartmentCycleError,
    DepartmentStatus,
    OrgSyncValidationError,
    RemoteDepartment,
    RemoteEmployee,
)
from infrastructure.feishu import (
    FeishuClient,
    FeishuConfig,
    FeishuConfigurationError,
    FeishuProtocolError,
    FeishuUpstreamError,
    FeishuUserIdentity,
)
from infrastructure.security.password import Argon2PasswordHasher
from main import create_app


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


class _FakeRedisStateBackend:
    """模拟 Redis SET EX 与原子 GETDEL，用于验证 OAuthStateStore 防重放行为。"""

    def __init__(self, *, now: list[float] | None = None) -> None:
        self.now = now if now is not None else [1000.0]
        self.store: dict[str, tuple[bytes, float]] = {}
        self.set_calls: list[tuple[str, int | None]] = []
        self.getdel_calls: list[str] = []
        self.error: Exception | None = None

    async def set(self, key: str, value: bytes, *, ex: int | None = None) -> bool:
        self.set_calls.append((key, ex))
        if self.error is not None:
            raise self.error
        ttl = float(ex) if ex is not None else 300.0
        self.store[key] = (value, self.now[0] + ttl)
        return True

    async def getdel(self, key: str) -> bytes | None:
        self.getdel_calls.append(key)
        if self.error is not None:
            raise self.error
        entry = self.store.pop(key, None)
        if entry is None:
            return None
        payload, expires_at = entry
        if self.now[0] >= expires_at:
            return None
        return payload


class _FakePasswordHasher:
    dummy_hash = "fake:dummy"

    def hash_password(self, password: str) -> str:
        return f"fake:{password}"

    def verify_password(self, password: str, password_hash: str) -> bool:
        return password_hash != self.dummy_hash and password_hash == f"fake:{password}"


async def _make_sqlite_db() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_factory


# ---------------------------------------------------------------------------
# 1. FeishuConfig 与 FeishuClient 安全性、缓存、重试、分页与有界响应体
# ---------------------------------------------------------------------------


def test_feishu_config_enforces_https_in_production_and_hides_secret() -> None:
    """生产环境强制 HTTPS，非生产允许测试 HTTP；repr 与错误消息不泄露 app_secret。"""
    secret = "cli_secret_sentinel_xyz"
    with pytest.raises(FeishuConfigurationError, match="feishu_base_url_invalid"):
        FeishuConfig.from_environment(
            {
                "ATHENA_ENV": "production",
                "ATHENA_FEISHU_APP_ID": "cli_app",
                "ATHENA_FEISHU_APP_SECRET": secret,
                "ATHENA_FEISHU_REDIRECT_URI": "https://app.example.com/api/v1/auth/feishu/callback",
                "ATHENA_FEISHU_BASE_URL": "http://127.0.0.1:9999",
            }
        )
    with pytest.raises(FeishuConfigurationError, match="feishu_redirect_uri_invalid"):
        FeishuConfig.from_environment(
            {
                "ATHENA_ENV": "production",
                "ATHENA_FEISHU_APP_ID": "cli_app",
                "ATHENA_FEISHU_APP_SECRET": secret,
                "ATHENA_FEISHU_REDIRECT_URI": "http://app.example.com/callback",
            }
        )

    dev_cfg = FeishuConfig.from_environment(
        {
            "ATHENA_ENV": "development",
            "ATHENA_FEISHU_APP_ID": "cli_app",
            "ATHENA_FEISHU_APP_SECRET": secret,
            "ATHENA_FEISHU_REDIRECT_URI": "http://dev.local:8000/api/v1/auth/feishu/callback?next=1",
            "ATHENA_FEISHU_BASE_URL": "http://127.0.0.1:18080",
        }
    )
    assert secret not in repr(dev_cfg)
    assert dev_cfg.base_url == "http://127.0.0.1:18080"
    assert dev_cfg.redirect_uri == "http://dev.local:8000/api/v1/auth/feishu/callback?next=1"

    # base_url 要求纯 origin：禁止路径段与非回环明文 HTTP。
    for bad_base_url in (
        "https://open.feishu.cn/proxy",
        "https://open.feishu.cn/v1/",
        "http://feishu-mock.internal:18080",
        "http://127.0.0.1:18080/api",
    ):
        with pytest.raises(FeishuConfigurationError, match="feishu_base_url_invalid"):
            FeishuConfig(
                app_id="cli_app",
                app_secret=secret,
                redirect_uri="https://app.example.com/api/v1/auth/feishu/callback",
                base_url=bad_base_url,
                allow_http_for_testing=True,
            )
        with pytest.raises(FeishuConfigurationError, match="feishu_base_url_invalid"):
            FeishuConfig.from_environment(
                {
                    "ATHENA_ENV": "development",
                    "ATHENA_FEISHU_APP_ID": "cli_app",
                    "ATHENA_FEISHU_APP_SECRET": secret,
                    "ATHENA_FEISHU_REDIRECT_URI": "http://dev.local:8000/callback",
                    "ATHENA_FEISHU_BASE_URL": bad_base_url,
                }
            )

    # 回环地址（localhost / [::1]）及带尾部斜杠的纯 HTTPS origin 应通过校验。
    for ok_base_url in (
        "https://open.feishu.cn/",
        "http://localhost:18080",
        "http://[::1]:18080",
    ):
        ok_cfg = FeishuConfig(
            app_id="cli_app",
            app_secret=secret,
            redirect_uri="http://dev.local:8000/api/v1/auth/feishu/callback",
            base_url=ok_base_url,
            allow_http_for_testing=True,
        )
        assert ok_cfg.base_url == ok_base_url


def test_feishu_client_caches_token_retries_429_5xx_and_never_leaks_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """验证 tenant_access_token 缓存与提前刷新、429/5xx 退避重试、分页与零凭据泄露。"""
    secret = "super-secret-feishu-app-secret"
    oauth_code = "one-time-oauth-code-secret"
    tenant_token_1 = "t-tenant-secret-1"
    tenant_token_2 = "t-tenant-secret-2"
    user_token = "u-user-access-token-secret"
    now = [100.0]
    token_requests = 0
    dept_attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal token_requests, dept_attempts
        path = request.url.path
        if path == "/open-apis/auth/v3/tenant_access_token/internal":
            token_requests += 1
            tok = tenant_token_1 if token_requests == 1 else tenant_token_2
            return httpx2.Response(
                200,
                json={"code": 0, "tenant_access_token": tok, "expire": 1000},
            )
        if path == "/open-apis/authen/v1/oidc/access_token":
            body = json.loads(request.content.decode("utf-8"))
            assert body["code"] == oauth_code
            return httpx2.Response(
                200,
                json={"code": 0, "data": {"access_token": user_token}},
            )
        if path == "/open-apis/authen/v1/user_info":
            assert request.headers["authorization"] == f"Bearer {user_token}"
            return httpx2.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "union_id": "on_u1",
                        "open_id": "ou_o1",
                        "name": "张三",
                        "enterprise_email": "ZhangSan@Corp.com",
                        "avatar_url": "https://cdn.example.com/a.png",
                        "employee_no": "E1001",
                        "job_title": "架构师",
                    },
                },
            )
        if path == "/open-apis/contact/v3/departments/0/children":
            dept_attempts += 1
            if dept_attempts == 1:
                return httpx2.Response(
                    429,
                    text=f"rate limited secret={secret}",
                    headers={"retry-after": "0.5"},
                )
            if dept_attempts == 2:
                return httpx2.Response(503, text=f"upstream error token={tenant_token_1}")
            page_token = request.url.params.get("page_token")
            if page_token is None:
                return httpx2.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "has_more": True,
                            "page_token": "p2",
                            "items": [
                                {
                                    "open_department_id": "od_root",
                                    "parent_department_id": "0",
                                    "name": "集团总部",
                                    "order": "1",
                                }
                            ],
                        },
                    },
                )
            return httpx2.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "items": [
                            {
                                "open_department_id": "od_rd",
                                "parent_department_id": "od_root",
                                "name": "研发部",
                                "order": 10,
                            }
                        ],
                    },
                },
            )
        if path == "/open-apis/contact/v3/users/find_by_department":
            return httpx2.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "items": [
                            {
                                "union_id": "on_u1",
                                "open_id": "ou_o1",
                                "name": "张三",
                                "email": "zhangsan@corp.com",
                                "employee_no": "E1001",
                                "job_title": "架构师",
                                "avatar": {"avatar_240": "https://cdn.example.com/a240.png"},
                                "department_ids": ["od_rd"],
                                "status": {"is_resigned": False, "is_frozen": False},
                            }
                        ],
                    },
                },
            )
        raise AssertionError(f"unexpected path: {path}")

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def scenario() -> None:
        config = FeishuConfig(
            app_id="cli_test",
            app_secret=secret,
            redirect_uri="https://app.example.com/api/v1/auth/feishu/callback",
        )
        client = FeishuClient(
            config,
            transport=httpx2.MockTransport(handler),
            sleep=fake_sleep,
            clock=lambda: now[0],
        )
        try:
            url = client.build_authorize_url("state_abc")
            parsed = urlsplit(url)
            qs = parse_qs(parsed.query)
            assert parsed.path == "/open-apis/authen/v1/authorize"
            assert qs["app_id"] == ["cli_test"]
            assert qs["state"] == ["state_abc"]

            # 1. 首次调用换取 tenant_access_token 并复用缓存。
            identity = await client.exchange_code_for_user(oauth_code)
            assert identity.union_id == "on_u1"
            assert identity.open_id == "ou_o1"
            assert identity.email == "ZhangSan@Corp.com"
            assert identity.employee_no == "E1001"
            assert identity.job_title == "架构师"
            assert token_requests == 1

            # 2. 拉取部门经过 429 + 503 重试后翻页成功，仍复用第一次 token。
            depts = await client.list_departments()
            assert [d.external_id for d in depts] == ["od_root", "od_rd"]
            assert len(sleeps) == 2
            assert token_requests == 1

            # 3. 推进时钟越过提前刷新点（1000 - 250 = 750s），应自动刷新 token。
            now[0] += 800.0
            users = await client.list_department_users("od_rd")
            assert len(users) == 1
            assert users[0].avatar_url == "https://cdn.example.com/a240.png"
            assert token_requests == 2
        finally:
            await client.aclose()

    with caplog.at_level(logging.DEBUG):
        _run(scenario())

    joined_logs = "\n".join(record.getMessage() for record in caplog.records)
    for sensitive in (secret, oauth_code, tenant_token_1, tenant_token_2, user_token):
        assert sensitive not in joined_logs


def test_feishu_client_rejects_redirects_and_masks_errors() -> None:
    """禁用重定向，且传输异常/上游错误绝不回显凭据。"""
    secret = "cli_secret_must_not_leak"

    async def scenario() -> None:
        config = FeishuConfig(
            app_id="cli_test",
            app_secret=secret,
            redirect_uri="https://app.example.com/callback",
        )
        # 1. 302 重定向必须被拒绝（follow_redirects=False）。
        redirect_client = FeishuClient(
            config,
            transport=httpx2.MockTransport(
                lambda _req: httpx2.Response(302, headers={"location": "https://evil.example/"})
            ),
            max_retries=0,
        )
        try:
            with pytest.raises(FeishuUpstreamError) as exc_info:
                await redirect_client.get_tenant_access_token()
            assert exc_info.value.status_code == 302
            assert secret not in str(exc_info.value)
        finally:
            await redirect_client.aclose()

        # 2. 底层异常包含敏感头时必须截断异常链。
        def timeout_handler(req: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout(f"timeout leaking {secret} {req.headers}")

        timeout_client = FeishuClient(
            config,
            transport=httpx2.MockTransport(timeout_handler),
            max_retries=0,
        )
        try:
            with pytest.raises(FeishuUpstreamError, match="feishu_timeout") as err:
                await timeout_client.get_tenant_access_token()
            assert secret not in str(err.value)
            assert err.value.__cause__ is None
        finally:
            await timeout_client.aclose()

    _run(scenario())


def test_feishu_client_exercises_all_five_endpoints_and_refreshes_expired_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """5 个 REST 端点全程直连有界 httpx2 客户端；99991663 触发单次强制刷新且零凭据泄露。"""
    secret = "cli_sync_guard_secret_xyz"
    oauth_code = "oauth-code-all-five-endpoints"
    tok_1 = "t-expired-secret-1"
    tok_2 = "t-refreshed-secret-2"
    user_tok = "u-all-five-secret"
    token_calls = 0
    dept_calls = 0
    seen_paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal token_calls, dept_calls
        path = request.url.path
        seen_paths.append(path)
        if path == "/open-apis/auth/v3/tenant_access_token/internal":
            token_calls += 1
            tok = tok_1 if token_calls == 1 else tok_2
            return httpx2.Response(
                200,
                json={"code": 0, "tenant_access_token": tok, "expire": 7200},
            )
        if path == "/open-apis/authen/v1/oidc/access_token":
            assert request.headers.get("authorization") == f"Bearer {tok_2}"
            return httpx2.Response(200, json={"code": 0, "data": {"access_token": user_tok}})
        if path == "/open-apis/authen/v1/user_info":
            assert request.headers.get("authorization") == f"Bearer {user_tok}"
            return httpx2.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "union_id": "on_sync_1",
                        "open_id": "ou_sync_1",
                        "name": "全路径用户",
                        "job_title": "工程师",
                    },
                },
            )
        if path == "/open-apis/contact/v3/departments/0/children":
            dept_calls += 1
            if request.headers.get("authorization") == f"Bearer {tok_1}":
                return httpx2.Response(
                    200,
                    json={
                        "code": 99991663,
                        "msg": f"token expired {tok_1} {secret}",
                        "data": "unexpected-shape-on-error",
                    },
                )
            assert request.headers.get("authorization") == f"Bearer {tok_2}"
            return httpx2.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "items": [
                            {
                                "open_department_id": "od_refreshed",
                                "parent_department_id": "0",
                                "name": "刷新后部门",
                                "order": 1,
                            }
                        ],
                    },
                },
            )
        if path == "/open-apis/contact/v3/users/find_by_department":
            assert request.headers.get("authorization") == f"Bearer {tok_2}"
            return httpx2.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "items": [
                            {
                                "union_id": "on_sync_1",
                                "open_id": "ou_sync_1",
                                "name": "全路径用户",
                                "department_ids": ["od_refreshed"],
                            }
                        ],
                    },
                },
            )
        raise AssertionError(f"unexpected path: {path}")

    async def scenario() -> None:
        config = FeishuConfig(
            app_id="cli_refresh",
            app_secret=secret,
            redirect_uri="https://app.example.com/callback",
        )
        client = FeishuClient(config, transport=httpx2.MockTransport(handler))
        try:
            departments = await client.list_departments()
            assert [d.external_id for d in departments] == ["od_refreshed"]
            assert token_calls == 2
            assert dept_calls == 2

            identity = await client.exchange_code_for_user(oauth_code)
            assert identity.open_id == "ou_sync_1"
            assert identity.job_title == "工程师"

            users = await client.list_department_users("od_refreshed")
            assert len(users) == 1
            assert users[0].open_id == "ou_sync_1"
            assert {
                "/open-apis/auth/v3/tenant_access_token/internal",
                "/open-apis/authen/v1/oidc/access_token",
                "/open-apis/authen/v1/user_info",
                "/open-apis/contact/v3/departments/0/children",
                "/open-apis/contact/v3/users/find_by_department",
            }.issubset(set(seen_paths))
            assert client._cached_token is not None
        finally:
            await client.aclose()
        # aclose 后一并清空缓存 token，避免关闭后残留明文凭据。
        assert client._cached_token is None

        # 若刷新后仍返回过期业务码，仅刷新重试一次后即失败，避免无限递归。
        always_expired_tokens = 0

        def always_expired(request: httpx2.Request) -> httpx2.Response:
            nonlocal always_expired_tokens
            if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
                always_expired_tokens += 1
                return httpx2.Response(
                    200,
                    json={
                        "code": 0,
                        "tenant_access_token": f"t-bad-{always_expired_tokens}",
                        "expire": 7200,
                    },
                )
            return httpx2.Response(200, json={"code": 99991663, "msg": "still expired"})

        failing_client = FeishuClient(config, transport=httpx2.MockTransport(always_expired))
        try:
            with pytest.raises(FeishuUpstreamError, match="feishu_api_error") as err:
                await failing_client.list_departments()
            assert err.value.api_code == 99991663
            assert always_expired_tokens == 2
        finally:
            await failing_client.aclose()

    with caplog.at_level(logging.DEBUG):
        _run(scenario())

    joined = "\n".join(record.getMessage() for record in caplog.records)
    for sensitive in (secret, oauth_code, tok_1, tok_2, user_tok):
        assert sensitive not in joined


def test_feishu_client_pagination_loop_protocol_guards_and_lifecycle() -> None:
    """验证分页环路拦截、429 Retry-After 重试、协议校验、并发隔离、取消透传与关闭后拒绝。"""
    config = FeishuConfig(
        app_id="cli_guard",
        app_secret="cli_guard_secret",
        redirect_uri="https://app.example.com/callback",
    )

    async def scenario() -> None:
        # 1. 分页 page_token 循环必须立即抛 feishu_pagination_loop_detected。
        def loop_handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
                return httpx2.Response(
                    200,
                    json={"code": 0, "tenant_access_token": "t-loop", "expire": 7200},
                )
            return httpx2.Response(
                200,
                json={
                    "code": 0,
                    "data": {"has_more": True, "page_token": "same_cursor", "items": []},
                },
            )

        loop_client = FeishuClient(config, transport=httpx2.MockTransport(loop_handler))
        try:
            with pytest.raises(FeishuProtocolError, match="feishu_pagination_loop_detected"):
                await loop_client.list_departments()
            with pytest.raises(FeishuProtocolError, match="feishu_pagination_loop_detected"):
                await loop_client.list_department_users("od_1")
        finally:
            await loop_client.aclose()

        # 2. 429 遵守 Retry-After 后重试成功。
        attempts = 0
        sleeps: list[float] = []

        def retry_handler(_request: httpx2.Request) -> httpx2.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx2.Response(429, headers={"retry-after": "1.5"}, text="slow down")
            return httpx2.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-ok", "expire": 7200},
            )

        async def record_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        retry_client = FeishuClient(
            config,
            transport=httpx2.MockTransport(retry_handler),
            sleep=record_sleep,
        )
        try:
            assert await retry_client.get_tenant_access_token() == "t-ok"
            assert sleeps == [1.5]
        finally:
            await retry_client.aclose()

        # 3. 200 响应协议边界：非法 JSON、损坏 gzip 解码与 bool code 均映射稳定协议异常。
        protocol_cases: list[tuple[dict[str, str], bytes, str]] = [
            ({"content-encoding": "gzip"}, b"corrupt-gzip", "feishu_response_decode_failed"),
            ({}, b"not-valid-json", "feishu_response_invalid_json"),
            (
                {},
                b'{"code": false, "tenant_access_token": "t", "expire": 10}',
                "feishu_response_code_invalid",
            ),
        ]
        for headers, body, expected_code in protocol_cases:
            p_client = FeishuClient(
                config,
                transport=httpx2.MockTransport(
                    lambda _req, hdrs=headers, payload=body: httpx2.Response(
                        200, headers=hdrs, content=payload
                    )
                ),
                max_retries=0,
            )
            try:
                with pytest.raises(FeishuProtocolError, match=expected_code):
                    await p_client.get_tenant_access_token()
            finally:
                await p_client.aclose()

        # 4. 并发 FeishuClient 实例各自持有独立连接池，互不串流。
        def make_token_transport(token_value: str) -> httpx2.MockTransport:
            async def slow_handler(_request: httpx2.Request) -> httpx2.Response:
                await asyncio.sleep(0.01)
                return httpx2.Response(
                    200,
                    json={"code": 0, "tenant_access_token": token_value, "expire": 7200},
                )

            return httpx2.MockTransport(slow_handler)

        client_a = FeishuClient(config, transport=make_token_transport("t-client-a"))
        client_b = FeishuClient(config, transport=make_token_transport("t-client-b"))
        try:
            tok_a, tok_b = await asyncio.gather(
                client_a.get_tenant_access_token(),
                client_b.get_tenant_access_token(),
            )
            assert tok_a == "t-client-a"
            assert tok_b == "t-client-b"
        finally:
            await client_a.aclose()
            await client_b.aclose()

        # 5. aclose 幂等且关闭后所有请求抛出 feishu_client_closed；CancelledError 不重试。
        await client_a.aclose()
        with pytest.raises(FeishuUpstreamError, match="feishu_client_closed"):
            await client_a.get_tenant_access_token()

        cancel_attempts = 0

        def cancel_handler(_request: httpx2.Request) -> httpx2.Response:
            nonlocal cancel_attempts
            cancel_attempts += 1
            raise asyncio.CancelledError()

        cancel_client = FeishuClient(config, transport=httpx2.MockTransport(cancel_handler))
        try:
            with pytest.raises(asyncio.CancelledError):
                await cancel_client.get_tenant_access_token()
            assert cancel_attempts == 1
        finally:
            await cancel_client.aclose()

    _run(scenario())


def test_feishu_client_rejects_null_items_and_non_retryable_status() -> None:
    """验证 data.items=[null] 映射为协议错，且非重试 HTTP 状态码映射为稳定上游错误。"""
    config = FeishuConfig(
        app_id="cli_null_items",
        app_secret="cli_null_secret",
        redirect_uri="https://app.example.com/callback",
    )

    def null_items_handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx2.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-null", "expire": 7200},
            )
        return httpx2.Response(
            200,
            json={"code": 0, "data": {"has_more": False, "items": [None]}},
        )

    async def scenario() -> None:
        # 1. items=[null] 不得触发未捕获 AttributeError，必须映射为稳定 FeishuProtocolError。
        client = FeishuClient(config, transport=httpx2.MockTransport(null_items_handler))
        try:
            with pytest.raises(FeishuProtocolError, match="feishu_department_item_invalid"):
                await client.list_departments()

            with pytest.raises(FeishuProtocolError, match="feishu_user_item_invalid"):
                await client.list_department_users("od_1")
        finally:
            await client.aclose()

        # 2. 非重试 400 状态码立即拒绝并映射为稳定上游错误。
        err_client = FeishuClient(
            config,
            transport=httpx2.MockTransport(lambda _req: httpx2.Response(400, text="bad")),
            max_retries=0,
        )
        try:
            with pytest.raises(FeishuUpstreamError, match="feishu_upstream_rejected"):
                await err_client.get_tenant_access_token()
        finally:
            await err_client.aclose()

    _run(scenario())


# ---------------------------------------------------------------------------
# 2. OAuthStateStore (Redis GETDEL / InMemory) 与安全 return_to 校验
# ---------------------------------------------------------------------------


def test_safe_return_to_validation_blocks_open_redirect_and_header_injection() -> None:
    assert validate_safe_return_to(None) == "/"
    assert validate_safe_return_to("") == "/"
    assert validate_safe_return_to("/") == "/"
    assert validate_safe_return_to("/chat/123?tab=files#top") == "/chat/123?tab=files#top"

    for unsafe in (
        "https://evil.example.com",
        "http://evil.example.com/a",
        "//evil.example.com/a",
        "/\\evil.example.com",
        "javascript:alert(1)",
        "/ok\r\nSet-Cookie: hacked=1",
        "/ok\x00bad",
        "relative/no-leading-slash",
    ):
        with pytest.raises(InvalidReturnToError):
            validate_safe_return_to(unsafe)


def test_redis_and_memory_oauth_state_store_are_single_use_and_fail_safe() -> None:
    """验证 Redis SET EX 300 + GETDEL 一次性消费防重放及故障 fail-safe。"""
    now = [1000.0]
    fake_redis = _FakeRedisStateBackend(now=now)
    redis_store = RedisOAuthStateStore(
        cast(Redis, fake_redis),
        state_factory=lambda: "fixed_state_1",
    )

    async def scenario() -> None:
        state = await redis_store.issue(provider="feishu", return_to="/workspace")
        assert state == "fixed_state_1"
        assert fake_redis.set_calls[-1] == ("athena:oauth:state:fixed_state_1", 300)

        first = await redis_store.consume(state, provider="feishu")
        assert first is not None
        assert first.return_to == "/workspace"
        assert fake_redis.getdel_calls[-1] == "athena:oauth:state:fixed_state_1"

        # 同一 state 重放必须返回 None。
        replay = await redis_store.consume(state, provider="feishu")
        assert replay is None

        # Redis 故障时抛 OAuthStateUnavailableError。
        fake_redis.error = RedisConnectionError("redis down")
        with pytest.raises(OAuthStateUnavailableError):
            await redis_store.issue(provider="feishu", return_to="/")

        # 内存实现同样具备一次性消费与过期控制。
        mem_store = InMemoryOAuthStateStore(ttl_seconds=10, clock=lambda: now[0])
        s1 = await mem_store.issue(provider="feishu", return_to="/projects")
        assert await mem_store.consume(s1, provider="other") is None
        s2 = await mem_store.issue(provider="feishu", return_to="/projects")
        now[0] += 11.0
        assert await mem_store.consume(s2, provider="feishu") is None

    _run(scenario())


def test_production_feishu_without_redis_blocks_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ATHENA_ENV=production 开启飞书但缺失 ATHENA_REDIS_URL 时拒绝启动。"""
    monkeypatch.setenv("ATHENA_ENV", "production")
    monkeypatch.setenv("ATHENA_FEISHU_ENABLED", "true")
    monkeypatch.setenv("ATHENA_FEISHU_APP_ID", "cli_prod")
    monkeypatch.setenv("ATHENA_FEISHU_APP_SECRET", "cli_prod_secret")
    monkeypatch.setenv(
        "ATHENA_FEISHU_REDIRECT_URI",
        "https://athena.example.com/api/v1/auth/feishu/callback",
    )
    monkeypatch.delenv("ATHENA_REDIS_URL", raising=False)
    monkeypatch.delenv("ATHENA_AUTH_MOCK_ENABLED", raising=False)

    with pytest.raises(RuntimeError, match="feishu_redis_state_store_required"):
        build_api_dependencies()


# ---------------------------------------------------------------------------
# 3. 飞书 SSO authorize / callback 全链路（SQLite + FastAPI TestClient）
# ---------------------------------------------------------------------------


def test_feishu_sso_authorize_callback_replay_and_disabled_revocation() -> None:
    """验证 authorize/callback 建户落库、防重放、角色保留及禁用/删除用户拒绝并撤销会话。"""
    now = datetime(2026, 11, 23, 8, 0, tzinfo=UTC)
    secret = "cli_callback_secret_999"
    current_identity: dict[str, Any] = {
        "union_id": "on_alice",
        "open_id": "ou_alice",
        "name": "Alice",
        "enterprise_email": "Alice@Corp.com",
        "avatar_url": "https://cdn.example.com/alice.png",
        "employee_no": "EMP001",
        "job_title": "财务总监",
    }
    fail_exchange = {"flag": False}

    def feishu_handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx2.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-secret-token", "expire": 7200},
            )
        if request.url.path == "/open-apis/authen/v1/oidc/access_token":
            if fail_exchange["flag"]:
                return httpx2.Response(
                    400,
                    text=f"invalid code with {secret}",
                )
            return httpx2.Response(
                200,
                json={"code": 0, "data": {"access_token": "u-secret-token"}},
            )
        if request.url.path == "/open-apis/authen/v1/user_info":
            return httpx2.Response(200, json={"code": 0, "data": current_identity})
        raise AssertionError(request.url.path)

    engine, session_factory = _run(_make_sqlite_db())
    try:
        auth_repo = SqlAlchemyAuthRepository(session_factory)
        org_repo = SqlAlchemyOrgRepository(session_factory)
        feishu_client = FeishuClient(
            FeishuConfig(
                app_id="cli_sso",
                app_secret=secret,
                redirect_uri="https://app.example.com/api/v1/auth/feishu/callback",
            ),
            transport=httpx2.MockTransport(feishu_handler),
        )
        state_store = InMemoryOAuthStateStore()
        auth_service = AuthService(
            auth_repo,
            _FakePasswordHasher(),
            org_reader=org_repo,
            feishu_client=feishu_client,
            oauth_state_store=state_store,
            clock=lambda: now,
        )
        app = create_app(auth_service=auth_service)
        with TestClient(app) as client:
            # 1. providers 声明 feishu_enabled=True。
            providers = client.get("/api/v1/auth/providers")
            assert providers.status_code == 200
            assert providers.json()["feishu_enabled"] is True

            # 2. 非法 return_to 被 400 拒绝。
            bad_return = client.get(
                "/api/v1/auth/feishu/authorize",
                params={"return_to": "https://evil.example.com"},
                follow_redirects=False,
            )
            assert bad_return.status_code == 400
            assert bad_return.json() == {"detail": "invalid_return_to"}

            # 3. 合法 authorize 返回 302 跳转飞书并携带一次性 state。
            authz = client.get(
                "/api/v1/auth/feishu/authorize",
                params={"return_to": "/chat?room=finance"},
                follow_redirects=False,
            )
            assert authz.status_code == 302
            location = authz.headers["location"]
            state = parse_qs(urlsplit(location).query)["state"][0]

            # 4. 首次 callback：自动创建 feishu 用户并签发 HttpOnly Cookie + 302 回跳。
            cb = client.get(
                "/api/v1/auth/feishu/callback",
                params={"code": "code_1", "state": state},
                follow_redirects=False,
            )
            assert cb.status_code == 302
            assert cb.headers["location"] == "/chat?room=finance"
            assert "httponly" in cb.headers["set-cookie"].lower()

            # 5. 重放同一 state 必须被 400 invalid_oauth_state 拒绝。
            replay = client.get(
                "/api/v1/auth/feishu/callback",
                params={"code": "code_1", "state": state},
                follow_redirects=False,
            )
            assert replay.status_code == 400
            assert replay.json() == {"detail": "invalid_oauth_state"}

            # 6. 凭签发的 Cookie 请求 /auth/me 返回完整资料。
            me = client.get("/api/v1/auth/me")
            assert me.status_code == 200
            me_data = me.json()
            assert me_data["email"] == "alice@corp.com"
            assert me_data["role"] == "member"
            assert me_data["auth_provider"] == "feishu"
            assert me_data["employee_no"] == "EMP001"
            assert me_data["job_title"] == "财务总监"
            assert me_data["avatar_url"] == "https://cdn.example.com/alice.png"
            alice_id = me_data["id"]

            # 7. 上游换码失败时返回脱敏 502，不泄露 app_secret。
            authz2 = client.get("/api/v1/auth/feishu/authorize", follow_redirects=False)
            state2 = parse_qs(urlsplit(authz2.headers["location"]).query)["state"][0]
            fail_exchange["flag"] = True
            failed_cb = client.get(
                "/api/v1/auth/feishu/callback",
                params={"code": "bad_code", "state": state2},
                follow_redirects=False,
            )
            assert failed_cb.status_code == 502
            assert failed_cb.json() == {"detail": "feishu_auth_failed"}
            assert secret not in failed_cb.text
            fail_exchange["flag"] = False

            # 8. 将 Alice 置为 disabled 后再次通过飞书回调：必须拒绝（403）并撤销既有会话！
            async def _disable_alice() -> None:
                async with session_factory() as s, s.begin():
                    row = await s.get(UserModel, alice_id)
                    assert row is not None
                    row.status = UserStatus.DISABLED.value

            _run(_disable_alice())
            authz3 = client.get("/api/v1/auth/feishu/authorize", follow_redirects=False)
            state3 = parse_qs(urlsplit(authz3.headers["location"]).query)["state"][0]
            disabled_cb = client.get(
                "/api/v1/auth/feishu/callback",
                params={"code": "code_disabled", "state": state3},
                follow_redirects=False,
            )
            assert disabled_cb.status_code == 403
            assert disabled_cb.json() == {"detail": "user_disabled"}

            # 验证 Alice 先前的 auth_sessions 已被真实提交撤销（而非被事务回滚）！
            async def _count_unrevoked_sessions() -> int:
                async with session_factory() as s:
                    rows = (
                        await s.scalars(
                            select(AuthSessionModel).where(
                                AuthSessionModel.user_id == alice_id,
                                AuthSessionModel.revoked_time.is_(None),
                            )
                        )
                    ).all()
                    return len(rows)

            assert _run(_count_unrevoked_sessions()) == 0
            assert client.get("/api/v1/auth/me").status_code == 401
        _run(feishu_client.aclose())
    finally:
        _run(engine.dispose())


# ---------------------------------------------------------------------------
# 4. 飞书通讯录组织树同步（物化路径、环路检测、离职自动吊销会话）
# ---------------------------------------------------------------------------


def test_department_cycle_detection_rejects_self_two_and_three_node_cycles() -> None:
    """自环、双向环和三节点环路必须在内存中立即被拒绝。"""
    # 自环 A -> A
    with pytest.raises(DepartmentCycleError):
        validate_and_order_departments(
            [RemoteDepartment(external_id="A", parent_external_id="A", name="自环")]
        )
    # 双向环 A <-> B
    with pytest.raises(DepartmentCycleError):
        validate_and_order_departments(
            [
                RemoteDepartment(external_id="A", parent_external_id="B", name="A"),
                RemoteDepartment(external_id="B", parent_external_id="A", name="B"),
            ]
        )
    # 三节点环 + 尾巴 D -> A -> B -> C -> A
    with pytest.raises(DepartmentCycleError):
        validate_and_order_departments(
            [
                RemoteDepartment(external_id="D", parent_external_id="A", name="D"),
                RemoteDepartment(external_id="A", parent_external_id="B", name="A"),
                RemoteDepartment(external_id="B", parent_external_id="C", name="B"),
                RemoteDepartment(external_id="C", parent_external_id="A", name="C"),
            ]
        )
    # 重复 external_id 拒绝
    with pytest.raises(OrgSyncValidationError, match="duplicate_department_external_id"):
        validate_and_order_departments(
            [
                RemoteDepartment(external_id="A", parent_external_id=None, name="A1"),
                RemoteDepartment(external_id="A", parent_external_id=None, name="A2"),
            ]
        )


def test_feishu_org_sync_computes_paths_preserves_db_on_cycle_and_revokes_departed() -> None:
    """验证多级部门物化路径、归档、环路不破坏现有树、离职停用并同事务吊销全部会话。"""
    now = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)

    class _StubDirectoryClient:
        def __init__(self) -> None:
            self.departments: list[RemoteDepartment] = []
            self.users_by_dept: dict[str, list[RemoteEmployee]] = {}

        async def list_departments(self) -> list[RemoteDepartment]:
            return list(self.departments)

        async def list_department_users(self, department_external_id: str) -> list[RemoteEmployee]:
            return list(self.users_by_dept.get(department_external_id, []))

    async def scenario() -> None:
        engine, session_factory = await _make_sqlite_db()
        try:
            auth_repo = SqlAlchemyAuthRepository(session_factory)
            org_repo = SqlAlchemyOrgRepository(session_factory)
            auth_service = AuthService(
                auth_repo,
                _FakePasswordHasher(),
                org_reader=org_repo,
                clock=lambda: now,
            )
            # 先置备一个本地应急超管，验证飞书同步绝不误伤 local 账号。
            local_admin, _ = await auth_service.bootstrap_super_admin(
                "local-admin@corp.com",
                "emergency-password-123",
            )
            local_login = await auth_service.login("local-admin@corp.com", "emergency-password-123")

            directory = _StubDirectoryClient()
            directory.departments = [
                RemoteDepartment("od_root", None, "集团总部", order_index=1),
                RemoteDepartment("od_rd", "od_root", "研发部", order_index=10),
                RemoteDepartment("od_plat", "od_rd", "平台架构组", order_index=11),
                RemoteDepartment("od_fin", "od_root", "财务部", order_index=20),
            ]
            directory.users_by_dept = {
                "od_plat": [
                    RemoteEmployee(
                        union_id="on_stay",
                        open_id="ou_stay",
                        name="留任研发",
                        email="stay@corp.com",
                        employee_no="E001",
                        job_title="工程师",
                        department_external_ids=("od_plat", "od_rd"),
                        is_active=True,
                    )
                ],
                "od_fin": [
                    RemoteEmployee(
                        union_id="on_leave",
                        open_id="ou_leave",
                        name="离职财务",
                        email="leave@corp.com",
                        employee_no="E002",
                        job_title="会计",
                        department_external_ids=("od_fin",),
                        is_active=True,
                    )
                ],
            }

            dept_seq = iter(["dept_root", "dept_rd", "dept_fin", "dept_plat"])
            user_seq = iter(["usr_sync_1", "usr_sync_2", "usr_sync_3"])
            sync_service = FeishuOrgSyncService(
                directory,
                org_repo,
                clock=lambda: now,
                department_id_factory=lambda: next(dept_seq),
                user_id_factory=lambda: next(user_seq),
            )

            # 1. 首次同步：验证多级部门物化路径与员工归属。
            first = await sync_service.sync()
            assert first.departments_upserted == 4
            assert first.departments_archived == 0
            assert first.users_upserted == 2
            assert first.users_disabled == 0

            depts = await org_repo.list_departments()
            by_ext = {d.external_id: d for d in depts}
            assert by_ext["od_root"].path == "/dept_root/"
            assert by_ext["od_rd"].path == "/dept_root/dept_rd/"
            assert by_ext["od_plat"].path == "/dept_root/dept_rd/dept_plat/"
            assert by_ext["od_fin"].path == "/dept_root/dept_fin/"

            stay_user = await auth_repo.get_user_by_email("stay@corp.com")
            leave_user = await auth_repo.get_user_by_email("leave@corp.com")
            assert stay_user is not None and leave_user is not None

            # 为离职员工与留任员工各签发真实 auth_sessions，同时将留任员工提权为 super_admin。
            leave_session_1, leave_tok_1, _ = auth_service._new_session(leave_user.id, now, 3600)
            leave_session_2, leave_tok_2, _ = auth_service._new_session(leave_user.id, now, 3600)
            stay_session, stay_tok, _ = auth_service._new_session(stay_user.id, now, 3600)
            await auth_repo.create_session(leave_session_1)
            await auth_repo.create_session(leave_session_2)
            await auth_repo.create_session(stay_session)
            await auth_repo.promote_active_user_by_email("stay@corp.com", now)

            # 2. 注入部门环路：必须被拒绝，且既有数据库部门树与用户会话毫发无损。
            directory.departments = [
                RemoteDepartment("od_root", "od_plat", "集团总部", order_index=1),
                RemoteDepartment("od_rd", "od_root", "研发部", order_index=10),
                RemoteDepartment("od_plat", "od_rd", "平台架构组", order_index=11),
            ]
            with pytest.raises(DepartmentCycleError):
                await sync_service.sync()
            assert len(await org_repo.list_departments()) == 4
            assert await auth_service.authenticate(leave_tok_1) is not None

            # 3. 二次正常同步：财务部被移除（archived），离职财务不在在职花名册中 ->
            #    同事务置 disabled 并撤销其全部 2 条活跃会话，留任员工保留 super_admin 角色！
            directory.departments = [
                RemoteDepartment("od_root", None, "集团总部", order_index=1),
                RemoteDepartment("od_rd", "od_root", "研发部", order_index=10),
                RemoteDepartment("od_plat", "od_rd", "平台架构组", order_index=11),
            ]
            directory.users_by_dept = {
                "od_plat": [
                    RemoteEmployee(
                        union_id="on_stay",
                        open_id="ou_stay",
                        name="留任研发（已晋升）",
                        email="stay@corp.com",
                        employee_no="E001",
                        job_title="资深架构师",
                        department_external_ids=("od_plat",),
                        is_active=True,
                    )
                ]
            }
            second = await sync_service.sync()
            assert second.departments_upserted == 3
            assert second.departments_archived == 1
            assert second.users_upserted == 1
            assert second.users_disabled == 1
            assert second.sessions_revoked == 2

            # 验证财务部已归档、离职员工 disabled 且全部会话已撤销。
            all_depts = {
                d.external_id: d for d in await org_repo.list_departments(include_archived=True)
            }
            assert all_depts["od_fin"].status is DepartmentStatus.ARCHIVED
            departed_user = await auth_repo.get_user_by_id(leave_user.id)
            assert departed_user is not None
            assert departed_user.status is UserStatus.DISABLED
            assert await auth_service.authenticate(leave_tok_1) is None
            assert await auth_service.authenticate(leave_tok_2) is None

            # 验证留任员工保留 super_admin 且会话仍有效；本地应急超管不受影响。
            stay_ctx = await auth_service.authenticate(stay_tok)
            assert stay_ctx is not None
            assert stay_ctx.user.role is UserRole.SUPER_ADMIN
            assert stay_ctx.user.display_name == "留任研发（已晋升）"
            assert await auth_service.authenticate(local_login.token) is not None
            assert (await auth_repo.get_user_by_id(local_admin.id)) is not None

            # 4. 验证用户部门表记录已更新为单部门。
            async with session_factory() as s:
                stay_bindings = (
                    await s.scalars(
                        select(UserDepartmentModel).where(
                            UserDepartmentModel.user_id == stay_user.id
                        )
                    )
                ).all()
                assert len(stay_bindings) == 1
                assert stay_bindings[0].department_id == "dept_plat"
                assert stay_bindings[0].is_primary is True

            # 5. 验证远端显式标记离职/冻结（is_active=False）时同样自动停用并撤销全部会话。
            directory.users_by_dept = {
                "od_plat": [
                    RemoteEmployee(
                        union_id="on_stay",
                        open_id="ou_stay",
                        name="留任研发（已离职）",
                        email="stay@corp.com",
                        employee_no="E001",
                        job_title="资深架构师",
                        department_external_ids=("od_plat",),
                        is_active=False,
                    )
                ]
            }
            third = await sync_service.sync()
            assert third.users_disabled == 1
            assert third.sessions_revoked == 1
            assert await auth_service.authenticate(stay_tok) is None
        finally:
            await engine.dispose()

    _run(scenario())


def test_bootstrap_super_admin_cli_helper_works_against_sqlite_file(
    tmp_path: Any,
) -> None:
    """验证 cli/bootstrap_super_admin.py 异步核心函数在真实 SQLite 库上创建与重置超管。"""
    db_file = tmp_path / "bootstrap.db"
    database_url = f"sqlite+aiosqlite:///{db_file}"

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

        user, created = await bootstrap_super_admin(
            email="Root@Corp.com",
            password="very-strong-password-1",
            database_url=database_url,
            display_name="应急超管",
        )
        assert created is True
        assert user.role is UserRole.SUPER_ADMIN
        assert user.auth_provider is AuthProvider.LOCAL
        assert user.email == "root@corp.com"

        reset_user, created_again = await bootstrap_super_admin(
            email="root@corp.com",
            password="very-strong-password-2",
            database_url=database_url,
        )
        assert created_again is False
        assert reset_user.id == user.id
        hasher = Argon2PasswordHasher()
        assert reset_user.password_hash is not None
        assert hasher.verify_password("very-strong-password-2", reset_user.password_hash)

    _run(scenario())


def test_sync_feishu_org_cli_helper_invokes_client_and_commits(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 cli/sync_feishu_org.py 核心入口函数可完整同步并正确关闭资源。"""
    db_file = tmp_path / "sync.db"
    database_url = f"sqlite+aiosqlite:///{db_file}"

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            return httpx2.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-tok", "expire": 7200},
            )
        if request.url.path == "/open-apis/contact/v3/departments/0/children":
            return httpx2.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "items": [
                            {
                                "open_department_id": "od_hq",
                                "parent_department_id": "0",
                                "name": "总部",
                                "order": 1,
                            }
                        ],
                    },
                },
            )
        if request.url.path == "/open-apis/contact/v3/users/find_by_department":
            return httpx2.Response(200, json={"code": 0, "data": {"has_more": False, "items": []}})
        raise AssertionError(request.url.path)

    original_init = FeishuClient.__init__

    def patched_init(self: FeishuClient, config: FeishuConfig, **kwargs: Any) -> None:
        kwargs.setdefault("transport", httpx2.MockTransport(handler))
        original_init(self, config, **kwargs)

    monkeypatch.setattr(FeishuClient, "__init__", patched_init)

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

        cfg = FeishuConfig(
            app_id="cli_sync",
            app_secret="cli_sync_secret",
            redirect_uri="https://app.example.com/callback",
        )
        summary = await run_feishu_org_sync(database_url=database_url, config=cfg)
        assert summary.departments_upserted == 1
        assert summary.departments_archived == 0

    _run(scenario())


# 占位消除未用导入提示（确认 DepartmentModel / build_auth_router / OrgService 可直接组合）。
_USED_SYMBOLS = (DepartmentModel, AuthCookieConfig, build_auth_router, OrgService, timedelta)


# ---------------------------------------------------------------------------
# 5. 熔断保护、第三方姓名容错清洗、双键合并防覆盖与子树归档过滤回归测试
# ---------------------------------------------------------------------------


def test_feishu_sync_aborts_on_empty_departments_or_empty_employees_fuse() -> None:
    """远端返回空部门或空员工名单时触发熔断保护，既有组织树、飞书员工、会话与 Mock 部门均完好。"""
    now = datetime(2026, 11, 23, 14, 0, tzinfo=UTC)

    class _FuseDirectoryStub:
        def __init__(self) -> None:
            self.departments: list[RemoteDepartment] = []
            self.users_by_dept: dict[str, list[RemoteEmployee]] = {}

        async def list_departments(self) -> list[RemoteDepartment]:
            return list(self.departments)

        async def list_department_users(self, department_external_id: str) -> list[RemoteEmployee]:
            return list(self.users_by_dept.get(department_external_id, []))

    engine, session_factory = _run(_make_sqlite_db())
    try:
        auth_repo = SqlAlchemyAuthRepository(session_factory)
        org_repo = SqlAlchemyOrgRepository(session_factory)
        auth_service = AuthService(
            auth_repo,
            _FakePasswordHasher(),
            org_reader=org_repo,
            clock=lambda: now,
        )
        # 1. 同时预置 Mock 部门与本地超管，验证飞书同步绝不误归档 mock_dept_* 部门。
        _run(seed_mock_organization(org_repo, clock=lambda: now))
        _run(auth_service.bootstrap_super_admin("boss@corp.com", "emergency-pass-123"))

        directory = _FuseDirectoryStub()
        directory.departments = [
            RemoteDepartment("od_root", None, "飞书总部", order_index=1),
            RemoteDepartment("od_sales", "od_root", "销售部", order_index=2),
        ]
        directory.users_by_dept = {
            "od_sales": [
                RemoteEmployee(
                    union_id="on_sales_1",
                    open_id="ou_sales_1",
                    name="销售甲",
                    email="sales1@corp.com",
                    department_external_ids=("od_sales",),
                    is_active=True,
                )
            ]
        }
        sync_service = FeishuOrgSyncService(directory, org_repo, clock=lambda: now)
        first = _run(sync_service.sync())
        assert first.departments_upserted == 2
        assert first.departments_archived == 0
        assert first.users_upserted == 1

        # 验证 Mock 部门全部保持 active，未被飞书同步归档。
        active_depts_before = _run(org_repo.list_departments())
        mock_dept_ids = [
            d.external_id for d in active_depts_before if d.external_id.startswith("mock_dept_")
        ]
        assert len(mock_dept_ids) == 5

        sales_user = _run(auth_repo.get_user_by_email("sales1@corp.com"))
        assert sales_user is not None
        sales_session, sales_token, _ = auth_service._new_session(sales_user.id, now, 3600)
        _run(auth_repo.create_session(sales_session))

        org_service = OrgService(
            org_repo,
            auth_repo,
            feishu_sync_service=sync_service,
            clock=lambda: now,
        )
        app = create_app(auth_service=auth_service, org_service=org_service)

        with TestClient(app) as client:
            assert (
                client.post(
                    "/api/v1/auth/login",
                    json={"email": "boss@corp.com", "password": "emergency-pass-123"},
                ).status_code
                == 200
            )

            # 2. 远端部门列表异常为空 -> 触发 empty_remote_departments 熔断（HTTP 422）。
            directory.departments = []
            with pytest.raises(OrgSyncValidationError, match="empty_remote_departments"):
                _run(sync_service.sync())
            empty_dept_resp = client.post("/api/v1/admin/org/sync")
            assert empty_dept_resp.status_code == 422
            assert empty_dept_resp.json() == {"detail": "empty_remote_departments"}

            # 3. 远端部门存在但所有部门返回空员工名单 -> 触发 empty_remote_employees 熔断（422）。
            directory.departments = [
                RemoteDepartment("od_root", None, "飞书总部", order_index=1),
                RemoteDepartment("od_sales", "od_root", "销售部", order_index=2),
            ]
            directory.users_by_dept = {}
            with pytest.raises(OrgSyncValidationError, match="empty_remote_employees"):
                _run(sync_service.sync())
            empty_emp_resp = client.post("/api/v1/admin/org/sync")
            assert empty_emp_resp.status_code == 422
            assert empty_emp_resp.json() == {"detail": "empty_remote_employees"}

        # 4. 两次熔断后，既有飞书活跃部门、活跃飞书员工及其活跃会话必须 100% 完好无损。
        active_depts_after = _run(org_repo.list_departments())
        assert {d.external_id for d in active_depts_after} == {
            d.external_id for d in active_depts_before
        }
        reloaded_sales = _run(auth_repo.get_user_by_id(sales_user.id))
        assert reloaded_sales is not None and reloaded_sales.status is UserStatus.ACTIVE
        assert _run(auth_service.authenticate(sales_token)) is not None
    finally:
        _run(engine.dispose())


def test_feishu_callback_sanitizes_c1_control_chars_and_overlong_display_names() -> None:
    """飞书回调遇到含 C1 控制字符（\\x85）或超长姓名时应容错清洗，不得 500 阻断登录。"""
    now = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)

    class _DirtyIdentitySsoClient:
        def __init__(self, identity: FeishuUserIdentity) -> None:
            self._identity = identity

        def build_authorize_url(self, state: str) -> str:
            return f"https://open.feishu.cn/open-apis/authen/v1/authorize?state={state}"

        async def exchange_code_for_user(self, code: str) -> FeishuUserIdentity:
            del code
            return self._identity

    engine, session_factory = _run(_make_sqlite_db())
    try:
        auth_repo = SqlAlchemyAuthRepository(session_factory)
        org_repo = SqlAlchemyOrgRepository(session_factory)

        # 1. AuthService 收到含 C1 控制字符 \x85 且超 80 字符的姓名 -> 自动剔除并截断为 80 字符。
        raw_dirty_name = f"  Alice\x85Zhang{'X' * 100}  "
        stub_client = _DirtyIdentitySsoClient(
            FeishuUserIdentity(
                union_id="on_dirty_1",
                open_id="ou_dirty_1",
                name=raw_dirty_name,
                email="dirty1@corp.com",
                avatar_url=None,
                employee_no=None,
                job_title=None,
            )
        )
        auth_service = AuthService(
            auth_repo,
            _FakePasswordHasher(),
            org_reader=org_repo,
            feishu_client=stub_client,
            oauth_state_store=InMemoryOAuthStateStore(),
            clock=lambda: now,
        )
        app = create_app(auth_service=auth_service)
        with TestClient(app) as client:
            authz = client.get("/api/v1/auth/feishu/authorize", follow_redirects=False)
            state = parse_qs(urlsplit(authz.headers["location"]).query)["state"][0]
            cb = client.get(
                "/api/v1/auth/feishu/callback",
                params={"code": "ok", "state": state},
                follow_redirects=False,
            )
            assert cb.status_code == 302
            me = client.get("/api/v1/auth/me")
            assert me.status_code == 200
            expected = f"AliceZhang{'X' * 100}"[:80]
            assert me.json()["display_name"] == expected
            assert len(me.json()["display_name"]) == 80
            assert "\x85" not in me.json()["display_name"]

        # 2. FeishuClient 层：name 含 C1 控制字符 \x85 时应被统一 Cc 规则识别并回退干净的 en_name。
        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
                return httpx2.Response(
                    200, json={"code": 0, "tenant_access_token": "t-ok", "expire": 7200}
                )
            if request.url.path == "/open-apis/authen/v1/oidc/access_token":
                return httpx2.Response(200, json={"code": 0, "data": {"access_token": "u-ok"}})
            if request.url.path == "/open-apis/authen/v1/user_info":
                return httpx2.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "union_id": "on_dirty_2",
                            "open_id": "ou_dirty_2",
                            "name": "Bad\x85Name",
                            "en_name": "Clean English Name",
                            "email": "dirty2@corp.com",
                        },
                    },
                )
            raise AssertionError(request.url.path)

        feishu_client = FeishuClient(
            FeishuConfig(
                app_id="cli_clean",
                app_secret="cli_clean_secret",
                redirect_uri="https://app.example.com/callback",
            ),
            transport=httpx2.MockTransport(handler),
        )
        try:
            identity = _run(feishu_client.exchange_code_for_user("code_clean"))
            assert identity.name == "Clean English Name"
        finally:
            _run(feishu_client.aclose())
    finally:
        _run(engine.dispose())


def test_merge_remote_employees_combines_separate_union_and_open_records_without_loss() -> None:
    """先 u:u1、再 o:o1、最后 (u1, o1) 双 ID 记录合并时，部门归属与资料字段不得被覆盖丢失。"""
    active, inactive = _merge_remote_employees(
        [
            RemoteEmployee(
                union_id="u1",
                open_id=None,
                name="张三",
                department_external_ids=("dept_a",),
                is_active=True,
            ),
            RemoteEmployee(
                union_id=None,
                open_id="o1",
                name=None,
                email="zhangsan@corp.com",
                employee_no="E100",
                department_external_ids=("dept_b",),
                is_active=True,
            ),
            RemoteEmployee(
                union_id="u1",
                open_id="o1",
                name=None,
                job_title="技术负责人",
                department_external_ids=("dept_c", "dept_a"),
                is_active=True,
            ),
        ]
    )
    assert inactive == []
    assert len(active) == 1
    merged = active[0]
    assert merged.union_id == "u1"
    assert merged.open_id == "o1"
    assert merged.name == "张三"
    assert merged.email == "zhangsan@corp.com"
    assert merged.employee_no == "E100"
    assert merged.job_title == "技术负责人"
    assert merged.department_external_ids == ("dept_a", "dept_b", "dept_c")

    # 顺带验证超长部门名（含 external_id 回退）按 String(120) 上限安全截断。
    long_ext = "od_" + "x" * 140
    ordered = validate_and_order_departments(
        [RemoteDepartment(external_id=long_ext, parent_external_id=None, name="   ")]
    )
    assert len(ordered) == 1
    assert len(ordered[0].name) == 120


def test_list_users_active_department_subtree_excludes_archived_descendants() -> None:
    """按活跃部门含子部门筛选用户时，自动排除仅归属已归档子部门的成员。"""
    now = datetime(2026, 11, 23, 16, 0, tzinfo=UTC)

    async def scenario() -> None:
        engine, session_factory = await _make_sqlite_db()
        try:
            org_repo = SqlAlchemyOrgRepository(session_factory)
            async with session_factory() as s, s.begin():
                s.add_all(
                    [
                        DepartmentModel(
                            id="dept_hq",
                            external_id="ext_hq",
                            parent_id=None,
                            name="总部",
                            path="/dept_hq/",
                            order_index=1,
                            status=DepartmentStatus.ACTIVE.value,
                            synced_at=now,
                            created_at=now,
                            updated_at=now,
                        ),
                        DepartmentModel(
                            id="dept_active_child",
                            external_id="ext_active_child",
                            parent_id="dept_hq",
                            name="活跃子部",
                            path="/dept_hq/dept_active_child/",
                            order_index=2,
                            status=DepartmentStatus.ACTIVE.value,
                            synced_at=now,
                            created_at=now,
                            updated_at=now,
                        ),
                        DepartmentModel(
                            id="dept_archived_child",
                            external_id="ext_archived_child",
                            parent_id="dept_hq",
                            name="已归档子部",
                            path="/dept_hq/dept_archived_child/",
                            order_index=3,
                            status=DepartmentStatus.ARCHIVED.value,
                            synced_at=now,
                            created_at=now,
                            updated_at=now,
                        ),
                        UserModel(
                            id="usr_in_active",
                            email="in-active@corp.com",
                            password_hash=None,
                            display_name="活跃子部员工",
                            status=UserStatus.ACTIVE.value,
                            role=UserRole.MEMBER.value,
                            auth_provider=AuthProvider.FEISHU.value,
                            created_at=now,
                            updated_at=now,
                        ),
                        UserModel(
                            id="usr_in_archived",
                            email="in-archived@corp.com",
                            password_hash=None,
                            display_name="归档子部员工",
                            status=UserStatus.ACTIVE.value,
                            role=UserRole.MEMBER.value,
                            auth_provider=AuthProvider.FEISHU.value,
                            created_at=now,
                            updated_at=now,
                        ),
                    ]
                )
                await s.flush()
                s.add_all(
                    [
                        UserDepartmentModel(
                            user_id="usr_in_active",
                            department_id="dept_active_child",
                            is_primary=True,
                        ),
                        UserDepartmentModel(
                            user_id="usr_in_archived",
                            department_id="dept_archived_child",
                            is_primary=True,
                        ),
                    ]
                )

            page = await org_repo.list_users(department_id="dept_hq", include_subdepartments=True)
            assert page.total == 1
            assert [item.user.id for item in page.items] == ["usr_in_active"]
        finally:
            await engine.dispose()

    _run(scenario())
