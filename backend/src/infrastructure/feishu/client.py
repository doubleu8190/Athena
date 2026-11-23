"""飞书开放平台原生 SSO 与通讯录异步 HTTP 客户端。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import os
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx2

from domain.org import FeishuDirectoryClient, RemoteDepartment, RemoteEmployee

logger = logging.getLogger(__name__)

DEFAULT_FEISHU_BASE_URL = "https://open.feishu.cn"
DEFAULT_FEISHU_TIMEOUT_SECONDS = 15.0
MIN_FEISHU_TIMEOUT_SECONDS = 0.1
MAX_FEISHU_TIMEOUT_SECONDS = 60.0
MAX_FEISHU_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_FEISHU_RETRIES = 2
MAX_FEISHU_PAGES = 1000
DEFAULT_FEISHU_PAGE_SIZE = 50
_TOKEN_REFRESH_LEAD_SECONDS = 300.0
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# 飞书服务端返回 tenant_access_token 失效/过期业务码时触发单次强制刷新。
_FEISHU_TOKEN_EXPIRED_CODES = frozenset({99991663, 99991664, 99991668})
_BASE_BACKOFF_SECONDS = 0.25
_MAX_BACKOFF_SECONDS = 5.0
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class FeishuError(RuntimeError):
    """飞书基础设施安全异常基类，绝不携带 app_secret、code 或 access_token。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class FeishuConfigurationError(FeishuError):
    """飞书环境变量缺失或格式非法。"""

    def __init__(self, code: str = "feishu_config_invalid") -> None:
        super().__init__(code)


class FeishuUpstreamError(FeishuError):
    """飞书开放平台 HTTP 请求失败、限流、超时或返回业务错误码。"""

    def __init__(
        self,
        code: str = "feishu_upstream_error",
        *,
        retryable: bool = False,
        status_code: int | None = None,
        api_code: int | None = None,
    ) -> None:
        self.retryable = retryable
        self.status_code = status_code
        self.api_code = api_code
        super().__init__(code)


class FeishuProtocolError(FeishuError):
    """飞书响应 JSON 结构非法、响应体超限或缺失必需身份字段。"""

    def __init__(self, code: str = "feishu_response_invalid") -> None:
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class FeishuUserIdentity:
    """通过飞书 OAuth code 换取的脱敏用户身份信息。"""

    union_id: str | None
    open_id: str | None
    name: str | None
    email: str | None
    avatar_url: str | None
    employee_no: str | None
    job_title: str | None


@dataclass(frozen=True, slots=True)
class FeishuConfig:
    """经过严格校验的飞书应用配置（repr 自动隐藏 app_secret）。"""

    app_id: str
    app_secret: str = field(repr=False)
    redirect_uri: str
    base_url: str = DEFAULT_FEISHU_BASE_URL
    timeout_seconds: float = DEFAULT_FEISHU_TIMEOUT_SECONDS
    allow_http_for_testing: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.app_id, str)
            or not self.app_id.strip()
            or len(self.app_id) > 128
            or _has_control_characters(self.app_id)
        ):
            raise FeishuConfigurationError("feishu_app_id_invalid")
        if (
            not isinstance(self.app_secret, str)
            or not self.app_secret.strip()
            or len(self.app_secret) > 256
            or _has_control_characters(self.app_secret)
        ):
            raise FeishuConfigurationError("feishu_app_secret_invalid")
        if not isinstance(self.allow_http_for_testing, bool):
            raise FeishuConfigurationError("feishu_config_invalid")
        if not isinstance(self.base_url, str):
            raise FeishuConfigurationError("feishu_base_url_invalid")
        _validate_feishu_url(
            self.base_url,
            allow_http=self.allow_http_for_testing,
            allow_query=False,
            code="feishu_base_url_invalid",
        )
        if not isinstance(self.redirect_uri, str):
            raise FeishuConfigurationError("feishu_redirect_uri_invalid")
        _validate_feishu_url(
            self.redirect_uri,
            allow_http=self.allow_http_for_testing,
            allow_query=True,
            code="feishu_redirect_uri_invalid",
        )
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or not MIN_FEISHU_TIMEOUT_SECONDS <= self.timeout_seconds <= MAX_FEISHU_TIMEOUT_SECONDS
        ):
            raise FeishuConfigurationError("feishu_timeout_invalid")

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        is_production: bool | None = None,
    ) -> FeishuConfig:
        """从 ATHENA_FEISHU_* 环境变量加载配置；生产环境强制校验 HTTPS。"""
        source = os.environ if environ is None else environ
        production = (
            source.get("ATHENA_ENV", "").strip().lower() == "production"
            if is_production is None
            else is_production
        )
        app_id = _require_secret_or_value(source, "ATHENA_FEISHU_APP_ID", "feishu_app_id_missing")
        app_secret = _require_secret_or_value(
            source,
            "ATHENA_FEISHU_APP_SECRET",
            "feishu_app_secret_missing",
        )
        redirect_uri = _require_secret_or_value(
            source,
            "ATHENA_FEISHU_REDIRECT_URI",
            "feishu_redirect_uri_missing",
        )
        raw_base_url = source.get("ATHENA_FEISHU_BASE_URL")
        if raw_base_url is None or not raw_base_url.strip():
            base_url = DEFAULT_FEISHU_BASE_URL
        else:
            if _has_control_characters(raw_base_url):
                raise FeishuConfigurationError("feishu_base_url_invalid")
            base_url = raw_base_url.strip().rstrip("/")
        return cls(
            app_id=app_id,
            app_secret=app_secret,
            redirect_uri=redirect_uri,
            base_url=base_url,
            allow_http_for_testing=not production,
        )


@dataclass(slots=True)
class _CachedTenantToken:
    """进程内缓存的 tenant_access_token 与过期单调时间戳。"""

    value: str = field(repr=False)
    expires_at: float


class FeishuClient(FeishuDirectoryClient):
    """基于有界 httpx2 连接池的飞书 OAuth SSO 与通讯录客户端。"""

    def __init__(
        self,
        config: FeishuConfig,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = monotonic,
        max_retries: int = MAX_FEISHU_RETRIES,
        max_response_bytes: int = MAX_FEISHU_RESPONSE_BYTES,
    ) -> None:
        if max_retries < 0 or max_retries > 5:
            raise ValueError("feishu_max_retries_invalid")
        if max_response_bytes < 1:
            raise ValueError("feishu_max_response_bytes_invalid")
        self._config = config
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._max_retries = max_retries
        self._max_response_bytes = max_response_bytes
        self._client: httpx2.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._token_lock = asyncio.Lock()
        self._cached_token: _CachedTenantToken | None = None
        self._closed = False

    @property
    def config(self) -> FeishuConfig:
        """返回只读配置对象（repr 已屏蔽 app_secret）。"""
        return self._config

    def build_authorize_url(self, state: str) -> str:
        """构造飞书 OAuth2 浏览器授权跳转地址。"""
        if not state or _has_control_characters(state):
            raise ValueError("feishu_state_invalid")
        query = urlencode(
            {
                "app_id": self._config.app_id,
                "redirect_uri": self._config.redirect_uri,
                "state": state,
            }
        )
        return f"{self._config.base_url.rstrip('/')}/open-apis/authen/v1/authorize?{query}"

    async def get_tenant_access_token(self, *, force_refresh: bool = False) -> str:
        """获取有效的 tenant_access_token，进程内缓存并在过期前自动刷新。"""
        now = self._clock()
        cached = self._cached_token
        if not force_refresh and cached is not None and now < cached.expires_at:
            return cached.value

        async with self._token_lock:
            now = self._clock()
            cached = self._cached_token
            if not force_refresh and cached is not None and now < cached.expires_at:
                return cached.value

            payload = await self._request_json(
                "POST",
                "/open-apis/auth/v3/tenant_access_token/internal",
                json_body={
                    "app_id": self._config.app_id,
                    "app_secret": self._config.app_secret,
                },
                operation="tenant_access_token",
            )
            token = payload.get("tenant_access_token")
            expire_raw = payload.get("expire")
            if (
                not isinstance(token, str)
                or not token.strip()
                or not isinstance(expire_raw, (int, float))
                or isinstance(expire_raw, bool)
                or not math.isfinite(expire_raw)
                or expire_raw <= 0
            ):
                raise FeishuProtocolError("feishu_token_response_invalid")

            # 提前刷新窗口取 300 秒或有效期的 1/4（较小值），避免临近过期时竞态失效。
            lead = min(_TOKEN_REFRESH_LEAD_SECONDS, float(expire_raw) * 0.25)
            effective_ttl = max(1.0, float(expire_raw) - lead)
            self._cached_token = _CachedTenantToken(
                value=token.strip(),
                expires_at=self._clock() + effective_ttl,
            )
            return self._cached_token.value

    async def exchange_code_for_user(self, code: str) -> FeishuUserIdentity:
        """使用一次性 OAuth code 换取飞书用户身份资料（不记录或回显敏感凭据）。"""
        if not code or not code.strip() or len(code) > 512 or _has_control_characters(code):
            raise FeishuProtocolError("feishu_oauth_code_invalid")

        token_payload = await self._request_tenant_json(
            "POST",
            "/open-apis/authen/v1/oidc/access_token",
            json_body={
                "grant_type": "authorization_code",
                "code": code.strip(),
            },
            operation="oidc_access_token",
        )
        token_data = token_payload.get("data")
        if not isinstance(token_data, dict):
            raise FeishuProtocolError("feishu_oidc_response_invalid")
        user_access_token = token_data.get("access_token")
        if not isinstance(user_access_token, str) or not user_access_token.strip():
            raise FeishuProtocolError("feishu_oidc_response_invalid")

        user_payload = await self._request_json(
            "GET",
            "/open-apis/authen/v1/user_info",
            headers={"Authorization": f"Bearer {user_access_token.strip()}"},
            operation="user_info",
        )
        user_data = user_payload.get("data")
        if not isinstance(user_data, dict):
            raise FeishuProtocolError("feishu_user_info_invalid")

        union_id = _optional_text(user_data.get("union_id"), max_length=128)
        open_id = _optional_text(user_data.get("open_id"), max_length=128)
        if union_id is None and open_id is None:
            raise FeishuProtocolError("feishu_user_identity_missing")

        name = _optional_text(user_data.get("name"), max_length=80) or _optional_text(
            user_data.get("en_name"),
            max_length=80,
        )
        email = _optional_text(user_data.get("enterprise_email"), max_length=320) or _optional_text(
            user_data.get("email"),
            max_length=320,
        )
        avatar_url = (
            _optional_text(user_data.get("avatar_url"), max_length=1024)
            or _optional_text(user_data.get("avatar_middle"), max_length=1024)
            or _optional_text(user_data.get("avatar_thumb"), max_length=1024)
        )
        employee_no = _optional_text(user_data.get("employee_no"), max_length=64)
        job_title = _optional_text(user_data.get("job_title"), max_length=120)
        return FeishuUserIdentity(
            union_id=union_id,
            open_id=open_id,
            name=name,
            email=email,
            avatar_url=avatar_url,
            employee_no=employee_no,
            job_title=job_title,
        )

    async def list_departments(self) -> list[RemoteDepartment]:
        """分页拉取应用可见范围内的全部飞书部门。"""
        departments: list[RemoteDepartment] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()

        for _ in range(MAX_FEISHU_PAGES):
            params: dict[str, str] = {
                "department_id_type": "open_department_id",
                "fetch_child": "true",
                "page_size": str(DEFAULT_FEISHU_PAGE_SIZE),
            }
            if page_token is not None:
                params["page_token"] = page_token
            payload = await self._request_tenant_json(
                "GET",
                "/open-apis/contact/v3/departments/0/children",
                params=params,
                operation="list_departments",
            )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise FeishuProtocolError("feishu_departments_response_invalid")
            items = data.get("items")
            if items is None:
                items = []
            if not isinstance(items, list):
                raise FeishuProtocolError("feishu_departments_response_invalid")

            for item in items:
                if not isinstance(item, dict):
                    raise FeishuProtocolError("feishu_department_item_invalid")
                departments.append(_parse_remote_department(item))

            has_more = bool(data.get("has_more", False))
            next_token = _optional_text(data.get("page_token"), max_length=512)
            if not has_more or next_token is None:
                return departments
            if next_token in seen_tokens:
                raise FeishuProtocolError("feishu_pagination_loop_detected")
            seen_tokens.add(next_token)
            page_token = next_token

        raise FeishuProtocolError("feishu_pagination_limit_exceeded")

    async def list_department_users(self, department_external_id: str) -> list[RemoteEmployee]:
        """分页拉取指定飞书部门下的直属员工列表。"""
        if not department_external_id or _has_control_characters(department_external_id):
            raise FeishuProtocolError("feishu_department_id_invalid")

        employees: list[RemoteEmployee] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()

        for _ in range(MAX_FEISHU_PAGES):
            params: dict[str, str] = {
                "department_id": department_external_id,
                "department_id_type": "open_department_id",
                "user_id_type": "open_id",
                "page_size": str(DEFAULT_FEISHU_PAGE_SIZE),
            }
            if page_token is not None:
                params["page_token"] = page_token
            payload = await self._request_tenant_json(
                "GET",
                "/open-apis/contact/v3/users/find_by_department",
                params=params,
                operation="list_department_users",
            )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise FeishuProtocolError("feishu_users_response_invalid")
            items = data.get("items")
            if items is None:
                items = []
            if not isinstance(items, list):
                raise FeishuProtocolError("feishu_users_response_invalid")

            for item in items:
                if not isinstance(item, dict):
                    raise FeishuProtocolError("feishu_user_item_invalid")
                employees.append(_parse_remote_employee(item, department_external_id))

            has_more = bool(data.get("has_more", False))
            next_token = _optional_text(data.get("page_token"), max_length=512)
            if not has_more or next_token is None:
                return employees
            if next_token in seen_tokens:
                raise FeishuProtocolError("feishu_pagination_loop_detected")
            seen_tokens.add(next_token)
            page_token = next_token

        raise FeishuProtocolError("feishu_pagination_limit_exceeded")

    async def aclose(self) -> None:
        """关闭底层有界 HTTP 连接池，可安全重复调用。"""
        async with self._client_lock:
            self._closed = True
            client = self._client
            self._client = None
        if client is not None:
            await client.aclose()

    def _build_client(self) -> httpx2.AsyncClient:
        """构造有界超时、有界连接池且禁止重定向的 httpx2 客户端，防止凭据跳转泄露。"""
        timeout = httpx2.Timeout(
            timeout=self._config.timeout_seconds,
            connect=min(10.0, self._config.timeout_seconds),
        )
        limits = httpx2.Limits(
            max_connections=32,
            max_keepalive_connections=16,
            keepalive_expiry=30.0,
        )
        kwargs: dict[str, Any] = {
            "timeout": timeout,
            "limits": limits,
            "follow_redirects": False,
            "headers": {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx2.AsyncClient(**kwargs)

    async def _ensure_client(self) -> httpx2.AsyncClient:
        """锁内获取或懒创建连接池，避免与并发关闭产生竞态。"""
        async with self._client_lock:
            if self._closed:
                raise FeishuUpstreamError("feishu_client_closed")
            if self._client is None:
                self._client = self._build_client()
            return self._client

    async def _request_tenant_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        operation: str,
    ) -> dict[str, Any]:
        """附带 tenant_access_token 发起请求；遇到 token 过期业务码时自动刷新并重试一次。"""
        token = await self.get_tenant_access_token()
        try:
            return await self._request_json(
                method,
                path,
                params=params,
                json_body=json_body,
                headers={"Authorization": f"Bearer {token}"},
                operation=operation,
            )
        except FeishuUpstreamError as error:
            if error.api_code in _FEISHU_TOKEN_EXPIRED_CODES:
                refreshed = await self.get_tenant_access_token(force_refresh=True)
                return await self._request_json(
                    method,
                    path,
                    params=params,
                    json_body=json_body,
                    headers={"Authorization": f"Bearer {refreshed}"},
                    operation=operation,
                )
            raise

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        operation: str,
    ) -> dict[str, Any]:
        """发起有界响应体与有界重试的飞书 API 请求，严格屏蔽任何凭据泄露。"""
        client = await self._ensure_client()
        endpoint = f"{self._config.base_url.rstrip('/')}{path}"
        request_headers = dict(headers) if headers is not None else {}
        if json_body is not None:
            request_headers["Content-Type"] = "application/json"

        for attempt in range(self._max_retries + 1):
            retry_delay: float | None = None
            try:
                async with client.stream(
                    method,
                    endpoint,
                    params=params,
                    json=json_body,
                    headers=request_headers,
                ) as response:
                    status_code = response.status_code
                    if 200 <= status_code < 300:
                        raw_body = await self._read_bounded_body(response)
                        return self._parse_feishu_envelope(raw_body, operation=operation)

                    if status_code in _RETRYABLE_STATUS_CODES:
                        if attempt < self._max_retries:
                            retry_delay = self._retry_delay(
                                attempt,
                                response.headers.get("retry-after"),
                            )
                            logger.warning(
                                "feishu_upstream_retry operation=%s status_code=%d "
                                "attempt=%d delay=%.2f",
                                operation,
                                status_code,
                                attempt + 1,
                                retry_delay,
                            )
                        else:
                            code = (
                                "feishu_rate_limited"
                                if status_code == 429
                                else "feishu_upstream_unavailable"
                            )
                            logger.warning(
                                "feishu_upstream_failed operation=%s code=%s status_code=%d",
                                operation,
                                code,
                                status_code,
                            )
                            raise FeishuUpstreamError(
                                code,
                                retryable=True,
                                status_code=status_code,
                            )
                    else:
                        # 非重试状态码（如 3xx 重定向、400/401/403）立刻拒绝，绝不回显响应内容。
                        logger.warning(
                            "feishu_upstream_failed operation=%s status_code=%d",
                            operation,
                            status_code,
                        )
                        raise FeishuUpstreamError(
                            "feishu_upstream_rejected",
                            retryable=False,
                            status_code=status_code,
                        )
            except asyncio.CancelledError:
                raise
            except FeishuError:
                raise
            except httpx2.TimeoutException:
                if attempt < self._max_retries:
                    retry_delay = self._retry_delay(attempt, None)
                    logger.warning(
                        "feishu_timeout_retry operation=%s attempt=%d delay=%.2f",
                        operation,
                        attempt + 1,
                        retry_delay,
                    )
                else:
                    logger.warning(
                        "feishu_upstream_failed operation=%s code=feishu_timeout",
                        operation,
                    )
                    # 截断底层 httpx2 异常链，防止请求体或 Authorization 头进入异常上下文。
                    raise FeishuUpstreamError("feishu_timeout", retryable=True) from None
            except httpx2.DecodingError as error:
                logger.warning(
                    "feishu_protocol_failed operation=%s exception_type=%s",
                    operation,
                    type(error).__name__,
                )
                raise FeishuProtocolError("feishu_response_decode_failed") from None
            except (httpx2.TransportError, httpx2.HTTPError, httpx2.StreamError) as error:
                logger.warning(
                    "feishu_upstream_failed operation=%s code=feishu_transport_error "
                    "exception_type=%s",
                    operation,
                    type(error).__name__,
                )
                raise FeishuUpstreamError("feishu_transport_error", retryable=False) from None

            if retry_delay is not None:
                await self._sleep(retry_delay)

        raise FeishuUpstreamError("feishu_upstream_unavailable", retryable=True)

    async def _read_bounded_body(self, response: httpx2.Response) -> bytes:
        """按字节上限流式读取响应体，防止异常上游耗尽进程内存。"""
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length.strip())
            except ValueError:
                raise FeishuProtocolError("feishu_response_length_invalid") from None
            if declared < 0 or declared > self._max_response_bytes:
                raise FeishuProtocolError("feishu_response_too_large")

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self._max_response_bytes:
                raise FeishuProtocolError("feishu_response_too_large")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _parse_feishu_envelope(raw_body: bytes, *, operation: str) -> dict[str, Any]:
        """校验飞书标准 JSON 响应结构与业务码 code == 0，不回显上游明文消息。"""
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise FeishuProtocolError("feishu_response_invalid_json") from None

        if not isinstance(payload, dict):
            raise FeishuProtocolError("feishu_response_invalid")
        api_code = payload.get("code")
        if not isinstance(api_code, int) or isinstance(api_code, bool):
            raise FeishuProtocolError("feishu_response_code_invalid")
        if api_code != 0:
            logger.warning(
                "feishu_api_error operation=%s api_code=%d",
                operation,
                api_code,
            )
            raise FeishuUpstreamError(
                "feishu_api_error",
                retryable=False,
                api_code=api_code,
            )
        return payload

    @staticmethod
    def _retry_delay(attempt: int, retry_after_header: str | None) -> float:
        """计算有界指数退避时间，并在合法范围内遵守上游 Retry-After。"""
        delay = float(min(_MAX_BACKOFF_SECONDS, _BASE_BACKOFF_SECONDS * (2**attempt)))
        if retry_after_header is not None:
            try:
                parsed = float(retry_after_header.strip())
            except ValueError:
                parsed = math.nan
            if math.isfinite(parsed) and parsed >= 0.0:
                delay = min(_MAX_BACKOFF_SECONDS, max(delay, parsed))
        return delay


def _parse_remote_department(item: Mapping[str, Any]) -> RemoteDepartment:
    """将飞书部门字典转换为领域 RemoteDepartment。"""
    external_id = _optional_text(item.get("open_department_id"), max_length=128) or _optional_text(
        item.get("department_id"),
        max_length=128,
    )
    name = _optional_text(item.get("name"), max_length=120)
    if external_id is None or name is None:
        raise FeishuProtocolError("feishu_department_item_invalid")
    raw_parent = _optional_text(item.get("parent_department_id"), max_length=128)
    parent_external_id = None if raw_parent in {None, "0"} else raw_parent

    raw_order = item.get("order", 0)
    if isinstance(raw_order, bool):
        order_index = 0
    elif isinstance(raw_order, int):
        order_index = raw_order
    elif isinstance(raw_order, str) and raw_order.strip():
        try:
            order_index = int(raw_order.strip())
        except ValueError:
            order_index = 0
    else:
        order_index = 0

    status_obj = item.get("status")
    is_deleted = False
    if isinstance(status_obj, dict):
        is_deleted = bool(status_obj.get("is_deleted", False))

    return RemoteDepartment(
        external_id=external_id,
        parent_external_id=parent_external_id,
        name=name,
        order_index=order_index,
        is_deleted=is_deleted,
    )


def _parse_remote_employee(
    item: Mapping[str, Any],
    queried_department_external_id: str,
) -> RemoteEmployee:
    """将飞书员工字典转换为领域 RemoteEmployee。"""
    union_id = _optional_text(item.get("union_id"), max_length=128)
    open_id = _optional_text(item.get("open_id"), max_length=128)
    if union_id is None and open_id is None:
        raise FeishuProtocolError("feishu_user_item_invalid")

    name = _optional_text(item.get("name"), max_length=80) or _optional_text(
        item.get("en_name"),
        max_length=80,
    )
    email = _optional_text(item.get("enterprise_email"), max_length=320) or _optional_text(
        item.get("email"),
        max_length=320,
    )
    employee_no = _optional_text(item.get("employee_no"), max_length=64)
    job_title = _optional_text(item.get("job_title"), max_length=120)

    avatar_url = _optional_text(item.get("avatar_url"), max_length=1024)
    avatar_obj = item.get("avatar")
    if avatar_url is None and isinstance(avatar_obj, dict):
        avatar_url = (
            _optional_text(avatar_obj.get("avatar_origin"), max_length=1024)
            or _optional_text(avatar_obj.get("avatar_240"), max_length=1024)
            or _optional_text(avatar_obj.get("avatar_72"), max_length=1024)
        )

    raw_department_ids = item.get("department_ids")
    dept_ids: list[str] = []
    if isinstance(raw_department_ids, list):
        for dept_id in raw_department_ids:
            cleaned = _optional_text(dept_id, max_length=128)
            if cleaned is not None and cleaned != "0" and cleaned not in dept_ids:
                dept_ids.append(cleaned)
    if not dept_ids and queried_department_external_id != "0":
        dept_ids.append(queried_department_external_id)

    is_active = True
    if isinstance(item.get("is_active"), bool):
        is_active = bool(item["is_active"])
    status_obj = item.get("status")
    if isinstance(status_obj, dict):
        if (
            bool(status_obj.get("is_resigned", False))
            or bool(status_obj.get("is_exited", False))
            or bool(status_obj.get("is_frozen", False))
        ):
            is_active = False

    return RemoteEmployee(
        union_id=union_id,
        open_id=open_id,
        name=name,
        email=email,
        employee_no=employee_no,
        avatar_url=avatar_url,
        job_title=job_title,
        department_external_ids=tuple(dept_ids),
        is_active=is_active,
    )


def _optional_text(value: object, *, max_length: int) -> str | None:
    """清理可选字符串字段，拒绝控制字符并截断到列安全上限。"""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or _has_control_characters(cleaned):
        return None
    return cleaned[:max_length]


def _require_secret_or_value(
    source: Mapping[str, str],
    name: str,
    missing_code: str,
) -> str:
    raw = source.get(name)
    if raw is None or not raw.strip():
        raise FeishuConfigurationError(missing_code)
    if _has_control_characters(raw):
        raise FeishuConfigurationError(missing_code.replace("_missing", "_invalid"))
    return raw.strip()


def _has_control_characters(value: str) -> bool:
    """检测全部 Unicode Cc 控制字符（含 C0/DEL/C1），防止请求头注入或日志污染。"""
    return any(unicodedata.category(ch) == "Cc" for ch in value)


def _validate_feishu_url(
    url: str,
    *,
    allow_http: bool,
    allow_query: bool,
    code: str,
) -> None:
    """校验飞书端点或回调地址：默认强制 HTTPS，禁止空白、控制字符与凭据片段。"""
    if not url or _has_control_characters(url) or any(ch.isspace() for ch in url) or "\\" in url:
        raise FeishuConfigurationError(code)
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise FeishuConfigurationError(code) from None

    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if (
        parsed.scheme.lower() not in allowed_schemes
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
        or parsed.fragment
        or (not allow_query and parsed.query)
    ):
        raise FeishuConfigurationError(code)

    trailing = parsed.netloc.rsplit("]", 1)[-1] if parsed.netloc.startswith("[") else parsed.netloc
    if (":" in trailing and port is None) or (port is not None and not 1 <= port <= 65535):
        raise FeishuConfigurationError(code)

    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        if host.replace(".", "").isdigit() and "." in host:
            raise FeishuConfigurationError(code) from None

    if len(host) > 253 or any(_HOST_LABEL.fullmatch(label) is None for label in host.split(".")):
        raise FeishuConfigurationError(code)


__all__ = [
    "DEFAULT_FEISHU_BASE_URL",
    "DEFAULT_FEISHU_PAGE_SIZE",
    "DEFAULT_FEISHU_TIMEOUT_SECONDS",
    "MAX_FEISHU_PAGES",
    "MAX_FEISHU_RESPONSE_BYTES",
    "MAX_FEISHU_RETRIES",
    "FeishuClient",
    "FeishuConfig",
    "FeishuConfigurationError",
    "FeishuError",
    "FeishuProtocolError",
    "FeishuUpstreamError",
    "FeishuUserIdentity",
]
