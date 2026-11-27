"""飞书开放平台原生 SSO 与通讯录异步客户端（基于标准 httpx2 与公共重试器）。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx2

from domain.auth import ExternalIdentityProviderError, FeishuSsoClient, FeishuUserIdentity
from domain.org import FeishuDirectoryClient, RemoteDepartment, RemoteEmployee
from infrastructure.http import (
    RetryableHttpError,
    has_control_characters,
    run_with_http_retry,
    validate_http_base_url,
)

logger = logging.getLogger(__name__)

DEFAULT_FEISHU_BASE_URL = "https://open.feishu.cn"
DEFAULT_FEISHU_TIMEOUT_SECONDS = 15.0
MIN_FEISHU_TIMEOUT_SECONDS = 0.1
MAX_FEISHU_TIMEOUT_SECONDS = 60.0
MAX_FEISHU_RETRIES = 2
MAX_FEISHU_PAGES = 1000
DEFAULT_FEISHU_PAGE_SIZE = 50
_TOKEN_REFRESH_LEAD_SECONDS = 300.0
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# 飞书服务端返回 tenant_access_token 失效/过期业务码时触发单次强制刷新。
_FEISHU_TOKEN_EXPIRED_CODES = frozenset({99991663, 99991664, 99991668})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class FeishuError(ExternalIdentityProviderError):
    """飞书基础设施安全异常基类，绝不携带 app_secret、code 或 access_token。"""

    def __init__(self, code: str) -> None:
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
    """飞书响应 JSON 结构非法或缺失必需身份字段。"""

    def __init__(self, code: str = "feishu_response_invalid") -> None:
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class FeishuConfig:
    """经过校验的飞书应用配置（repr 自动隐藏 app_secret）。"""

    app_id: str
    app_secret: str = field(repr=False)
    redirect_uri: str
    base_url: str = DEFAULT_FEISHU_BASE_URL
    timeout_seconds: float = DEFAULT_FEISHU_TIMEOUT_SECONDS
    allow_http_for_testing: bool = False

    def __post_init__(self) -> None:
        if not self.app_id.strip() or len(self.app_id) > 128 or _has_control_chars(self.app_id):
            raise FeishuConfigurationError("feishu_app_id_invalid")
        if (
            not self.app_secret.strip()
            or len(self.app_secret) > 256
            or _has_control_chars(self.app_secret)
        ):
            raise FeishuConfigurationError("feishu_app_secret_invalid")
        _validate_feishu_url(
            self.base_url,
            allow_http=self.allow_http_for_testing,
            allow_query=False,
            require_pure_origin=True,
            code="feishu_base_url_invalid",
        )
        _validate_feishu_url(
            self.redirect_uri,
            allow_http=self.allow_http_for_testing,
            allow_query=True,
            require_pure_origin=False,
            code="feishu_redirect_uri_invalid",
        )
        if (
            isinstance(self.timeout_seconds, bool)
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
        raw_base = (source.get("ATHENA_FEISHU_BASE_URL") or "").strip().rstrip("/")
        return cls(
            app_id=_require_env_value(source, "ATHENA_FEISHU_APP_ID", "feishu_app_id_missing"),
            app_secret=_require_env_value(
                source, "ATHENA_FEISHU_APP_SECRET", "feishu_app_secret_missing"
            ),
            redirect_uri=_require_env_value(
                source, "ATHENA_FEISHU_REDIRECT_URI", "feishu_redirect_uri_missing"
            ),
            base_url=raw_base or DEFAULT_FEISHU_BASE_URL,
            allow_http_for_testing=not production,
        )


@dataclass(slots=True)
class _CachedTenantToken:
    """进程内缓存的 tenant_access_token 与过期单调时间戳。"""

    value: str = field(repr=False)
    expires_at: float


class FeishuClient(FeishuDirectoryClient, FeishuSsoClient):
    """基于标准 httpx2 客户端与公共重试器的飞书 SSO 与通讯录 REST 客户端。"""

    def __init__(
        self,
        config: FeishuConfig,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = monotonic,
        max_retries: int = MAX_FEISHU_RETRIES,
    ) -> None:
        if max_retries < 0 or max_retries > 5:
            raise ValueError("feishu_max_retries_invalid")
        self._config = config
        self._base_url = config.base_url.rstrip("/")
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._max_retries = max_retries
        self._http_client: httpx2.AsyncClient | None = None
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
        if not state or _has_control_chars(state):
            raise ValueError("feishu_state_invalid")
        query = urlencode(
            {
                "app_id": self._config.app_id,
                "redirect_uri": self._config.redirect_uri,
                "state": state,
            }
        )
        return f"{self._base_url}/open-apis/authen/v1/authorize?{query}"

    async def get_tenant_access_token(self, *, force_refresh: bool = False) -> str:
        """获取自建应用 tenant_access_token，进程内缓存并在过期前自动刷新。"""
        self._ensure_open()
        if (
            not force_refresh
            and self._cached_token is not None
            and self._clock() < self._cached_token.expires_at
        ):
            return self._cached_token.value

        async with self._token_lock:
            self._ensure_open()
            if (
                not force_refresh
                and self._cached_token is not None
                and self._clock() < self._cached_token.expires_at
            ):
                return self._cached_token.value

            payload = await self._request_json(
                "POST",
                "/open-apis/auth/v3/tenant_access_token/internal",
                operation="tenant_access_token",
                json_body={
                    "app_id": self._config.app_id,
                    "app_secret": self._config.app_secret,
                },
            )
            # 飞书内部 token 接口通常返回顶层字段，兼容回退读取 data 字典。
            data_fallback: Mapping[str, Any] = (
                payload["data"] if isinstance(payload.get("data"), Mapping) else {}
            )
            token = payload.get("tenant_access_token", data_fallback.get("tenant_access_token"))
            expire_raw = payload.get("expire", data_fallback.get("expire"))
            if (
                not isinstance(token, str)
                or not token.strip()
                or _has_control_chars(token)
                or not isinstance(expire_raw, (int, float))
                or isinstance(expire_raw, bool)
                or not math.isfinite(expire_raw)
                or expire_raw <= 0
            ):
                raise FeishuProtocolError("feishu_token_response_invalid")

            self._ensure_open()
            # 提前刷新窗口取 300 秒或有效期的 1/4（较小值），避免临近过期时竞态失效。
            effective_ttl = max(
                1.0, float(expire_raw) - min(_TOKEN_REFRESH_LEAD_SECONDS, float(expire_raw) * 0.25)
            )
            self._cached_token = _CachedTenantToken(
                value=token.strip(),
                expires_at=self._clock() + effective_ttl,
            )
            return self._cached_token.value

    async def exchange_code_for_user(self, code: str) -> FeishuUserIdentity:
        """使用一次性 OAuth code 换取飞书用户身份资料（不记录或回显敏感凭据）。"""
        self._ensure_open()
        if not code or not code.strip() or len(code) > 512 or _has_control_chars(code):
            raise FeishuProtocolError("feishu_oauth_code_invalid")

        oidc_data = (
            await self._request_tenant_json(
                "POST",
                "/open-apis/authen/v1/oidc/access_token",
                operation="oidc_access_token",
                json_body={"grant_type": "authorization_code", "code": code.strip()},
            )
        ).get("data")
        user_token = oidc_data.get("access_token") if isinstance(oidc_data, Mapping) else None
        if (
            not isinstance(user_token, str)
            or not user_token.strip()
            or _has_control_chars(user_token)
        ):
            raise FeishuProtocolError("feishu_oidc_response_invalid")

        user_data = (
            await self._request_json(
                "GET",
                "/open-apis/authen/v1/user_info",
                operation="user_info",
                headers={"Authorization": f"Bearer {user_token.strip()}"},
            )
        ).get("data")
        if not isinstance(user_data, Mapping):
            raise FeishuProtocolError("feishu_user_info_invalid")

        union_id = _optional_text(user_data.get("union_id"), max_length=128)
        open_id = _optional_text(user_data.get("open_id"), max_length=128)
        if union_id is None and open_id is None:
            raise FeishuProtocolError("feishu_user_identity_missing")
        return FeishuUserIdentity(
            union_id=union_id,
            open_id=open_id,
            name=_first_text(user_data, ("name", "en_name"), max_length=80),
            email=_first_text(user_data, ("enterprise_email", "email"), max_length=320),
            avatar_url=_first_text(
                user_data, ("avatar_url", "avatar_middle", "avatar_thumb"), max_length=1024
            ),
            employee_no=_optional_text(user_data.get("employee_no"), max_length=64),
            job_title=_optional_text(user_data.get("job_title"), max_length=120),
        )

    async def list_departments(self) -> list[RemoteDepartment]:
        """分页拉取应用可见范围内的全部飞书部门。"""
        return await self._paginate_tenant(
            path="/open-apis/contact/v3/departments/0/children",
            base_params={
                "department_id_type": "open_department_id",
                "fetch_child": "true",
                "page_size": str(DEFAULT_FEISHU_PAGE_SIZE),
            },
            operation="list_departments",
            invalid_response_code="feishu_departments_response_invalid",
            invalid_item_code="feishu_department_item_invalid",
            parse_item=_parse_remote_department,
        )

    async def list_department_users(self, department_external_id: str) -> list[RemoteEmployee]:
        """分页拉取指定飞书部门下的直属员工列表。"""
        self._ensure_open()
        if not department_external_id or _has_control_chars(department_external_id):
            raise FeishuProtocolError("feishu_department_id_invalid")
        return await self._paginate_tenant(
            path="/open-apis/contact/v3/users/find_by_department",
            base_params={
                "department_id": department_external_id,
                "department_id_type": "open_department_id",
                "user_id_type": "open_id",
                "page_size": str(DEFAULT_FEISHU_PAGE_SIZE),
            },
            operation="list_department_users",
            invalid_response_code="feishu_users_response_invalid",
            invalid_item_code="feishu_user_item_invalid",
            parse_item=lambda item: _parse_remote_employee(item, department_external_id),
        )

    async def aclose(self) -> None:
        """关闭底层 HTTP 连接池并清空缓存 token，可安全重复调用。"""
        async with self._client_lock:
            self._closed = True
            self._cached_token = None
            http_client = self._http_client
            self._http_client = None
        if http_client is not None:
            await http_client.aclose()

    def _ensure_open(self) -> None:
        if self._closed:
            raise FeishuUpstreamError("feishu_client_closed")

    async def _ensure_http_client(self) -> httpx2.AsyncClient:
        """在锁内原子懒创建标准 httpx2 异步客户端（禁重定向、有界超时与连接池）。"""
        async with self._client_lock:
            self._ensure_open()
            if self._http_client is None:
                timeout = httpx2.Timeout(
                    timeout=self._config.timeout_seconds,
                    connect=min(10.0, self._config.timeout_seconds),
                )
                self._http_client = httpx2.AsyncClient(
                    timeout=timeout,
                    limits=httpx2.Limits(
                        max_connections=32,
                        max_keepalive_connections=16,
                        keepalive_expiry=30.0,
                    ),
                    follow_redirects=False,
                    headers={"Accept": "application/json"},
                    transport=self._transport,
                )
            return self._http_client

    async def _paginate_tenant[ItemT](
        self,
        *,
        path: str,
        base_params: Mapping[str, str],
        operation: str,
        invalid_response_code: str,
        invalid_item_code: str,
        parse_item: Callable[[Mapping[str, Any]], ItemT],
    ) -> list[ItemT]:
        """执行带环路检测与最大页数上限保护的飞书游标分页查询。"""
        self._ensure_open()
        results: list[ItemT] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()

        for _ in range(MAX_FEISHU_PAGES):
            params = dict(base_params)
            if page_token is not None:
                params["page_token"] = page_token
            data = (
                await self._request_tenant_json("GET", path, operation=operation, params=params)
            ).get("data")
            if not isinstance(data, Mapping):
                raise FeishuProtocolError(invalid_response_code)
            raw_items = data.get("items")
            if raw_items is not None and not isinstance(raw_items, list):
                raise FeishuProtocolError(invalid_response_code)
            for raw_item in raw_items or ():
                if not isinstance(raw_item, Mapping):
                    raise FeishuProtocolError(invalid_item_code)
                results.append(parse_item(raw_item))

            next_token = _optional_text(data.get("page_token"), max_length=512)
            if not bool(data.get("has_more")) or next_token is None:
                return results
            if next_token in seen_tokens:
                raise FeishuProtocolError("feishu_pagination_loop_detected")
            seen_tokens.add(next_token)
            page_token = next_token

        raise FeishuProtocolError("feishu_pagination_limit_exceeded")

    async def _request_tenant_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """附带 tenant_access_token 发起请求；遇过期业务码时单次强制刷新后重试。"""
        for force_refresh in (False, True):
            token = await self.get_tenant_access_token(force_refresh=force_refresh)
            try:
                return await self._request_json(
                    method,
                    path,
                    operation=operation,
                    params=params,
                    json_body=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except FeishuUpstreamError as error:
                if not force_refresh and error.api_code in _FEISHU_TOKEN_EXPIRED_CODES:
                    continue
                raise
        raise FeishuUpstreamError("feishu_upstream_error")  # pragma: no cover

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """复用公共 tenacity 重试器执行单次飞书 REST 调用，所有异常均脱敏截断。"""
        http_client = await self._ensure_http_client()
        url = f"{self._base_url}{path}"

        def _on_retry(attempt_number: int, delay: float, error: RetryableHttpError) -> None:
            logger.warning(
                "feishu_upstream_retry operation=%s status_code=%s attempt=%d delay=%.2f",
                operation,
                error.status_code,
                attempt_number,
                delay,
            )

        async def _sleep_if_open(seconds: float) -> None:
            self._ensure_open()
            await self._sleep(seconds)
            self._ensure_open()

        async def _attempt(_attempt_index: int) -> dict[str, Any]:
            self._ensure_open()
            try:
                response = await http_client.request(
                    method, url, headers=headers, params=params, json=json_body
                )
            except asyncio.CancelledError:
                raise
            except httpx2.TimeoutException:
                # 截断底层 httpx2 异常链，防止请求体或 Authorization 头进入异常上下文。
                raise RetryableHttpError(
                    final_error=FeishuUpstreamError("feishu_timeout", retryable=True)
                ) from None
            except httpx2.DecodingError as error:
                logger.warning(
                    "feishu_protocol_failed operation=%s exception_type=%s",
                    operation,
                    type(error).__name__,
                )
                raise FeishuProtocolError("feishu_response_decode_failed") from None
            except (httpx2.TransportError, httpx2.HTTPError, httpx2.StreamError) as error:
                self._ensure_open()
                logger.warning(
                    "feishu_upstream_failed operation=%s code=feishu_transport_error "
                    "exception_type=%s",
                    operation,
                    type(error).__name__,
                )
                raise FeishuUpstreamError("feishu_transport_error", retryable=False) from None
            except RuntimeError:
                self._ensure_open()
                raise

            if not response.is_success:
                status_code = response.status_code
                if status_code in _RETRYABLE_STATUS_CODES:
                    code = (
                        "feishu_rate_limited"
                        if status_code == 429
                        else "feishu_upstream_unavailable"
                    )
                    raise RetryableHttpError(
                        retry_after_header=response.headers.get("retry-after"),
                        final_error=FeishuUpstreamError(
                            code, retryable=True, status_code=status_code
                        ),
                        status_code=status_code,
                    )
                logger.warning(
                    "feishu_upstream_failed operation=%s status_code=%d", operation, status_code
                )
                raise FeishuUpstreamError(
                    "feishu_upstream_rejected", retryable=False, status_code=status_code
                )
            return _parse_feishu_envelope(response.content, operation=operation)

        try:
            return await run_with_http_retry(
                _attempt,
                max_retries=self._max_retries,
                sleep=_sleep_if_open,
                on_retry=_on_retry,
            )
        except FeishuUpstreamError as error:
            if error.retryable:
                logger.warning(
                    "feishu_upstream_failed operation=%s code=%s status_code=%s",
                    operation,
                    error.code,
                    error.status_code,
                )
            raise


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
        logger.warning("feishu_api_error operation=%s api_code=%d", operation, api_code)
        raise FeishuUpstreamError("feishu_api_error", retryable=False, api_code=api_code)
    return payload


def _parse_remote_department(item: Mapping[str, Any]) -> RemoteDepartment:
    """将飞书部门 JSON 字典转换为领域 RemoteDepartment。"""
    external_id = _first_text(item, ("open_department_id", "department_id"), max_length=128)
    name = _optional_text(item.get("name"), max_length=120)
    if external_id is None or name is None:
        raise FeishuProtocolError("feishu_department_item_invalid")
    raw_parent = _optional_text(item.get("parent_department_id"), max_length=128)
    raw_order = item.get("order", 0)
    order_index = 0
    if isinstance(raw_order, int) and not isinstance(raw_order, bool):
        order_index = raw_order
    elif isinstance(raw_order, str) and raw_order.strip():
        try:
            order_index = int(raw_order.strip())
        except ValueError:
            order_index = 0
    status_obj = item.get("status")
    return RemoteDepartment(
        external_id=external_id,
        parent_external_id=None if raw_parent in {None, "0"} else raw_parent,
        name=name,
        order_index=order_index,
        is_deleted=bool(status_obj.get("is_deleted")) if isinstance(status_obj, Mapping) else False,
    )


def _parse_remote_employee(
    item: Mapping[str, Any],
    queried_department_external_id: str,
) -> RemoteEmployee:
    """将飞书用户 JSON 字典转换为领域 RemoteEmployee。"""
    union_id = _optional_text(item.get("union_id"), max_length=128)
    open_id = _optional_text(item.get("open_id"), max_length=128)
    if union_id is None and open_id is None:
        raise FeishuProtocolError("feishu_user_item_invalid")

    avatar_url = _optional_text(item.get("avatar_url"), max_length=1024)
    avatar_obj = item.get("avatar")
    if avatar_url is None and isinstance(avatar_obj, Mapping):
        avatar_url = _first_text(
            avatar_obj, ("avatar_origin", "avatar_240", "avatar_72"), max_length=1024
        )

    dept_ids: list[str] = []
    if isinstance(item.get("department_ids"), list):
        for dept_id in item["department_ids"]:
            cleaned = _optional_text(dept_id, max_length=128)
            if cleaned is not None and cleaned != "0" and cleaned not in dept_ids:
                dept_ids.append(cleaned)
    if not dept_ids and queried_department_external_id != "0":
        dept_ids.append(queried_department_external_id)

    status_obj = item.get("status")
    is_active = bool(item["is_active"]) if isinstance(item.get("is_active"), bool) else True
    if item.get("is_frozen") is True or (
        isinstance(status_obj, Mapping)
        and any(bool(status_obj.get(flag)) for flag in ("is_resigned", "is_exited", "is_frozen"))
    ):
        is_active = False

    return RemoteEmployee(
        union_id=union_id,
        open_id=open_id,
        name=_first_text(item, ("name", "en_name"), max_length=80),
        email=_first_text(item, ("enterprise_email", "email"), max_length=320),
        employee_no=_optional_text(item.get("employee_no"), max_length=64),
        avatar_url=avatar_url,
        job_title=_optional_text(item.get("job_title"), max_length=120),
        department_external_ids=tuple(dept_ids),
        is_active=is_active,
    )


def _first_text(source: Mapping[str, Any], keys: tuple[str, ...], *, max_length: int) -> str | None:
    for key in keys:
        cleaned = _optional_text(source.get(key), max_length=max_length)
        if cleaned is not None:
            return cleaned
    return None


def _optional_text(value: object, *, max_length: int) -> str | None:
    """清理可选字符串字段，拒绝控制字符并截断到列安全上限。"""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or _has_control_chars(cleaned):
        return None
    return cleaned[:max_length]


def _require_env_value(source: Mapping[str, str], name: str, missing_code: str) -> str:
    raw = source.get(name)
    if raw is None or not raw.strip():
        raise FeishuConfigurationError(missing_code)
    if _has_control_chars(raw):
        raise FeishuConfigurationError(missing_code.replace("_missing", "_invalid"))
    return raw.strip()


def _has_control_chars(value: str) -> bool:
    """检测 ASCII 及 Unicode Cc 控制字符，防止请求头注入或日志污染。"""
    if has_control_characters(value):
        return True
    return not value.isascii() and any(unicodedata.category(ch) == "Cc" for ch in value)


def _validate_feishu_url(
    url: str,
    *,
    allow_http: bool,
    allow_query: bool,
    require_pure_origin: bool,
    code: str,
) -> None:
    """校验飞书接口或回调地址；base_url 要求纯 origin 且测试明文 HTTP 仅限回环地址。"""
    if not url or _has_control_chars(url):
        raise FeishuConfigurationError(code)
    try:
        validate_http_base_url(url, allow_http=allow_http, allow_query=allow_query)
    except ValueError:
        raise FeishuConfigurationError(code) from None
    if not require_pure_origin:
        return
    parsed = urlsplit(url.rstrip("/"))
    if parsed.path != "" or (
        parsed.scheme.lower() == "http" and (parsed.hostname or "").lower() not in _LOOPBACK_HOSTS
    ):
        raise FeishuConfigurationError(code)


__all__ = [
    "DEFAULT_FEISHU_BASE_URL",
    "DEFAULT_FEISHU_PAGE_SIZE",
    "DEFAULT_FEISHU_TIMEOUT_SECONDS",
    "MAX_FEISHU_PAGES",
    "MAX_FEISHU_RETRIES",
    "FeishuClient",
    "FeishuConfig",
    "FeishuConfigurationError",
    "FeishuError",
    "FeishuProtocolError",
    "FeishuUpstreamError",
]
