"""飞书开放平台原生 SSO 与通讯录异步客户端（基于官方 lark-oapi 异步 SDK）。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx2
import lark_oapi as lark
from lark_oapi.api.auth.v3.model import (
    InternalTenantAccessTokenRequest,
    InternalTenantAccessTokenRequestBody,
    InternalTenantAccessTokenResponse,
)
from lark_oapi.api.authen.v1.model import (
    CreateOidcAccessTokenRequest,
    CreateOidcAccessTokenRequestBody,
    CreateOidcAccessTokenResponse,
    GetUserInfoRequest,
    GetUserInfoResponse,
)
from lark_oapi.api.contact.v3.model import (
    ChildrenDepartmentRequest,
    ChildrenDepartmentResponse,
    Department,
    FindByDepartmentUserRequest,
    FindByDepartmentUserResponse,
    User,
)
from lark_oapi.core.exception import (
    AccessDeniedException,
    AccessTokenException,
    ClientAssertionException,
    InvalidArgsException,
    NoAuthorizationException,
    ObtainAccessTokenException,
    UnmarshalException,
)
from lark_oapi.core.http import Transport as LarkTransport
from lark_oapi.core.http.transport import (
    _build_header as _lark_build_header,
)
from lark_oapi.core.http.transport import (
    _build_url as _lark_build_url,
)
from lark_oapi.core.json import JSON as LARK_JSON
from lark_oapi.core.model import BaseRequest, Config, RawResponse, RequestOption

from domain.org import FeishuDirectoryClient, RemoteDepartment, RemoteEmployee
from infrastructure.http import (
    RetryableHttpError,
    build_bounded_async_client,
    has_control_characters,
    run_with_http_retry,
    validate_http_base_url,
)

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
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


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


class _FeishuHttpStatusSignal(Exception):
    """内部控制异常：在 lark_oapi 执行 JSON 反序列化前拦截非 2xx 状态码并保留 Retry-After。"""

    def __init__(self, *, status_code: int, retry_after_header: str | None) -> None:
        self.status_code = status_code
        self.retry_after_header = retry_after_header
        super().__init__("feishu_http_status")


@dataclass(slots=True)
class _BoundedLarkExecutionContext:
    """绑定到当前协程的飞书有界 HTTP 客户端、体积上限与解析后的原始信封快照。"""

    http_client: httpx2.AsyncClient
    max_response_bytes: int
    operation: str
    last_payload: dict[str, Any] | None = None


# 通过 ContextVar 将 lark_oapi.Transport.aexecute 按协程隔离桥接到 FeishuClient 自持有的
# 有界连接池 httpx2.AsyncClient，既复用连接池、流式限制响应体积、在反序列化前拦截非 2xx
# 响应，又不影响同进程内其他独立使用 lark_oapi 的调用方。
_ACTIVE_LARK_CONTEXT: ContextVar[_BoundedLarkExecutionContext | None] = ContextVar(
    "athena_feishu_lark_context",
    default=None,
)
_ORIGINAL_LARK_AEXECUTE: Callable[..., Awaitable[RawResponse]] = LarkTransport.aexecute
_LARK_BRIDGE_LOCK = threading.Lock()
_LARK_BRIDGE_INSTALLED = False


async def _read_bounded_success_body(
    response: httpx2.Response,
    *,
    max_response_bytes: int,
) -> bytes:
    """在 2xx 响应阶段强制校验编码与字节上限，防止压缩炸弹或超大响应体耗尽内存。"""
    content_encoding = response.headers.get("content-encoding", "").strip().lower()
    if content_encoding not in {"", "identity"}:
        raise FeishuProtocolError("feishu_response_decode_failed")

    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length.strip())
        except ValueError:
            raise FeishuProtocolError("feishu_response_length_invalid") from None
        if declared < 0:
            raise FeishuProtocolError("feishu_response_length_invalid")
        if declared > max_response_bytes:
            raise FeishuProtocolError("feishu_response_too_large")

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_response_bytes:
            raise FeishuProtocolError("feishu_response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _bridged_lark_aexecute(
    conf: Config,
    req: BaseRequest,
    option: RequestOption | None = None,
) -> RawResponse:
    """拦截 lark_oapi 异步传输：若在 FeishuClient 上下文内则走有界 httpx2 连接池。"""
    ctx = _ACTIVE_LARK_CONTEXT.get()
    if ctx is None:
        return await _ORIGINAL_LARK_AEXECUTE(conf, req, option)

    effective_option = RequestOption() if option is None else option
    url = _lark_build_url(conf.domain, req.uri, req.paths)
    headers = dict(_lark_build_header(req, effective_option, conf))

    json_body: Any = None
    if req.body is not None:
        marshaled = LARK_JSON.marshal(req.body)
        if marshaled is not None:
            json_body = json.loads(marshaled)

    async with ctx.http_client.stream(
        str(req.http_method.name),
        url,
        headers=headers,
        params=req.queries or None,
        json=json_body,
    ) as response:
        status_code = response.status_code
        if not (200 <= status_code < 300):
            # 非 2xx 响应不读取错误正文（0 字节读取并随 async with 关闭流），避免大错误体
            # 或非 JSON 响应破坏 lark_oapi 反序列化并杜绝凭据回显。
            raise _FeishuHttpStatusSignal(
                status_code=status_code,
                retry_after_header=response.headers.get("retry-after"),
            )
        raw_body = await _read_bounded_success_body(
            response,
            max_response_bytes=ctx.max_response_bytes,
        )
        # 在返回给 lark_oapi 反序列化前先校验标准 JSON 信封与业务码（过滤 bool 伪装 0，
        # 且在 code != 0 时直接抛出安全业务异常而不回显上游 msg）。
        ctx.last_payload = _parse_feishu_envelope(raw_body, operation=ctx.operation)
        raw_resp = RawResponse()
        raw_resp.status_code = status_code
        raw_resp.headers = dict(response.headers)
        raw_resp.content = raw_body
        return raw_resp


def _ensure_lark_transport_bridge() -> None:
    """幂等安装 lark_oapi.Transport.aexecute 的协程隔离有界传输桥接。"""
    global _LARK_BRIDGE_INSTALLED
    if _LARK_BRIDGE_INSTALLED:
        return
    with _LARK_BRIDGE_LOCK:
        if _LARK_BRIDGE_INSTALLED:
            return
        LarkTransport.aexecute = staticmethod(_bridged_lark_aexecute)
        _LARK_BRIDGE_INSTALLED = True


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
        _validate_feishu_base_url(
            self.base_url,
            allow_http=self.allow_http_for_testing,
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
    """基于官方异步 lark-oapi SDK 与共享有界 HTTP 基座的飞书 SSO 与通讯录客户端。"""

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
        self._sdk_client: lark.Client | None = None
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
        """通过 SDK 异步接口获取 tenant_access_token，进程内缓存并在过期前自动刷新。"""
        if self._closed:
            raise FeishuUpstreamError("feishu_client_closed")
        now = self._clock()
        cached = self._cached_token
        if not force_refresh and cached is not None and now < cached.expires_at:
            return cached.value

        async with self._token_lock:
            if self._closed:
                raise FeishuUpstreamError("feishu_client_closed")
            now = self._clock()
            cached = self._cached_token
            if not force_refresh and cached is not None and now < cached.expires_at:
                return cached.value

            async def _call(sdk: lark.Client) -> InternalTenantAccessTokenResponse:
                request = (
                    InternalTenantAccessTokenRequest.builder()
                    .request_body(
                        InternalTenantAccessTokenRequestBody.builder()
                        .app_id(self._config.app_id)
                        .app_secret(self._config.app_secret)
                        .build()
                    )
                    .build()
                )
                return await sdk.auth.v3.tenant_access_token.ainternal(request)

            sdk_resp, raw_payload = await self._invoke_sdk(
                _call,
                operation="tenant_access_token",
                invalid_data_code="feishu_token_response_invalid",
            )
            # 飞书 /auth/v3/tenant_access_token/internal 返回顶层字段（SDK 模型嵌套在 data 下），
            # 优先读取原始 JSON 顶层字段并在缺失时回退 SDK data 对象。
            token = raw_payload.get("tenant_access_token")
            expire_raw = raw_payload.get("expire")
            if token is None and sdk_resp.data is not None:
                token = sdk_resp.data.tenant_access_token
            if expire_raw is None and sdk_resp.data is not None:
                expire_raw = sdk_resp.data.expire

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
            if self._closed:
                raise FeishuUpstreamError("feishu_client_closed")
            lead = min(_TOKEN_REFRESH_LEAD_SECONDS, float(expire_raw) * 0.25)
            effective_ttl = max(1.0, float(expire_raw) - lead)
            self._cached_token = _CachedTenantToken(
                value=token.strip(),
                expires_at=self._clock() + effective_ttl,
            )
            return self._cached_token.value

    async def exchange_code_for_user(self, code: str) -> FeishuUserIdentity:
        """使用一次性 OAuth code 换取飞书用户身份资料（不记录或回显敏感凭据）。"""
        if self._closed:
            raise FeishuUpstreamError("feishu_client_closed")
        if not code or not code.strip() or len(code) > 512 or _has_control_characters(code):
            raise FeishuProtocolError("feishu_oauth_code_invalid")

        cleaned_code = code.strip()

        async def _call_oidc(
            sdk: lark.Client,
            tenant_token: str,
        ) -> CreateOidcAccessTokenResponse:
            request = (
                CreateOidcAccessTokenRequest.builder()
                .request_body(
                    CreateOidcAccessTokenRequestBody.builder()
                    .grant_type("authorization_code")
                    .code(cleaned_code)
                    .build()
                )
                .build()
            )
            # CreateOidcAccessTokenRequest 声明 token_types={APP}；自建应用使用
            # tenant_access_token 作为 Bearer 凭据，经 app_access_token 注入可命中
            # enable_set_token 异步直通分支，严禁触发同步阻塞的 TokenManager。
            option = RequestOption.builder().app_access_token(tenant_token).build()
            return await sdk.authen.v1.oidc_access_token.acreate(request, option)

        oidc_resp, oidc_payload = await self._invoke_tenant_sdk(
            _call_oidc,
            operation="oidc_access_token",
            invalid_data_code="feishu_oidc_response_invalid",
        )
        if not isinstance(oidc_payload.get("data"), dict) or oidc_resp.data is None:
            raise FeishuProtocolError("feishu_oidc_response_invalid")
        user_access_token = oidc_resp.data.access_token
        if not isinstance(user_access_token, str) or not user_access_token.strip():
            raise FeishuProtocolError("feishu_oidc_response_invalid")

        cleaned_user_token = user_access_token.strip()

        async def _call_user_info(sdk: lark.Client) -> GetUserInfoResponse:
            request = GetUserInfoRequest.builder().build()
            option = RequestOption.builder().user_access_token(cleaned_user_token).build()
            return await sdk.authen.v1.user_info.aget(request, option)

        user_resp, user_payload = await self._invoke_sdk(
            _call_user_info,
            operation="user_info",
            invalid_data_code="feishu_user_info_invalid",
        )
        raw_user_data = user_payload.get("data")
        if not isinstance(raw_user_data, dict) or user_resp.data is None:
            raise FeishuProtocolError("feishu_user_info_invalid")

        user_data = user_resp.data
        union_id = _optional_text(user_data.union_id, max_length=128)
        open_id = _optional_text(user_data.open_id, max_length=128)
        if union_id is None and open_id is None:
            raise FeishuProtocolError("feishu_user_identity_missing")

        name = _optional_text(user_data.name, max_length=80) or _optional_text(
            user_data.en_name,
            max_length=80,
        )
        email = _optional_text(user_data.enterprise_email, max_length=320) or _optional_text(
            user_data.email,
            max_length=320,
        )
        avatar_url = (
            _optional_text(user_data.avatar_url, max_length=1024)
            or _optional_text(user_data.avatar_middle, max_length=1024)
            or _optional_text(user_data.avatar_thumb, max_length=1024)
        )
        employee_no = _optional_text(user_data.employee_no, max_length=64)
        # GetUserInfoResponseBody 未建模 job_title 字段，因此从校验后的原始 data 字典补充读取。
        job_title = _optional_text(raw_user_data.get("job_title"), max_length=120)
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
        """通过 SDK 异步分页接口拉取应用可见范围内的全部飞书部门。"""
        if self._closed:
            raise FeishuUpstreamError("feishu_client_closed")
        departments: list[RemoteDepartment] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()

        for _ in range(MAX_FEISHU_PAGES):
            current_page_token = page_token

            async def _call_page(
                sdk: lark.Client,
                tenant_token: str,
                token_arg: str | None = current_page_token,
            ) -> ChildrenDepartmentResponse:
                builder = (
                    ChildrenDepartmentRequest.builder()
                    .department_id("0")
                    .department_id_type("open_department_id")
                    .fetch_child(True)
                    .page_size(DEFAULT_FEISHU_PAGE_SIZE)
                )
                if token_arg is not None:
                    builder = builder.page_token(token_arg)
                option = RequestOption.builder().tenant_access_token(tenant_token).build()
                return await sdk.contact.v3.department.achildren(builder.build(), option)

            sdk_resp, raw_payload = await self._invoke_tenant_sdk(
                _call_page,
                operation="list_departments",
                invalid_data_code="feishu_departments_response_invalid",
                invalid_item_code="feishu_department_item_invalid",
            )
            raw_data = raw_payload.get("data")
            if not isinstance(raw_data, dict) or sdk_resp.data is None:
                raise FeishuProtocolError("feishu_departments_response_invalid")
            raw_items = raw_data.get("items")
            if raw_items is not None and not isinstance(raw_items, list):
                raise FeishuProtocolError("feishu_departments_response_invalid")

            sdk_items = sdk_resp.data.items or []
            for sdk_dept in sdk_items:
                if not isinstance(sdk_dept, Department):
                    raise FeishuProtocolError("feishu_department_item_invalid")
                departments.append(_parse_remote_department(sdk_dept))

            has_more = bool(sdk_resp.data.has_more)
            next_token = _optional_text(sdk_resp.data.page_token, max_length=512)
            if not has_more or next_token is None:
                return departments
            if next_token in seen_tokens:
                raise FeishuProtocolError("feishu_pagination_loop_detected")
            seen_tokens.add(next_token)
            page_token = next_token

        raise FeishuProtocolError("feishu_pagination_limit_exceeded")

    async def list_department_users(self, department_external_id: str) -> list[RemoteEmployee]:
        """通过 SDK 异步分页接口拉取指定飞书部门下的直属员工列表。"""
        if self._closed:
            raise FeishuUpstreamError("feishu_client_closed")
        if not department_external_id or _has_control_characters(department_external_id):
            raise FeishuProtocolError("feishu_department_id_invalid")

        employees: list[RemoteEmployee] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()

        for _ in range(MAX_FEISHU_PAGES):
            current_page_token = page_token

            async def _call_page(
                sdk: lark.Client,
                tenant_token: str,
                token_arg: str | None = current_page_token,
            ) -> FindByDepartmentUserResponse:
                builder = (
                    FindByDepartmentUserRequest.builder()
                    .department_id(department_external_id)
                    .department_id_type("open_department_id")
                    .user_id_type("open_id")
                    .page_size(DEFAULT_FEISHU_PAGE_SIZE)
                )
                if token_arg is not None:
                    builder = builder.page_token(token_arg)
                option = RequestOption.builder().tenant_access_token(tenant_token).build()
                return await sdk.contact.v3.user.afind_by_department(builder.build(), option)

            sdk_resp, raw_payload = await self._invoke_tenant_sdk(
                _call_page,
                operation="list_department_users",
                invalid_data_code="feishu_users_response_invalid",
                invalid_item_code="feishu_user_item_invalid",
            )
            raw_data = raw_payload.get("data")
            if not isinstance(raw_data, dict) or sdk_resp.data is None:
                raise FeishuProtocolError("feishu_users_response_invalid")
            raw_items = raw_data.get("items")
            if raw_items is None:
                raw_items = []
            if not isinstance(raw_items, list):
                raise FeishuProtocolError("feishu_users_response_invalid")

            sdk_items = sdk_resp.data.items or []
            for index, sdk_user in enumerate(sdk_items):
                if not isinstance(sdk_user, User):
                    raise FeishuProtocolError("feishu_user_item_invalid")
                raw_item = raw_items[index] if index < len(raw_items) else {}
                if not isinstance(raw_item, Mapping):
                    raise FeishuProtocolError("feishu_user_item_invalid")
                employees.append(_parse_remote_employee(sdk_user, raw_item, department_external_id))

            has_more = bool(sdk_resp.data.has_more)
            next_token = _optional_text(sdk_resp.data.page_token, max_length=512)
            if not has_more or next_token is None:
                return employees
            if next_token in seen_tokens:
                raise FeishuProtocolError("feishu_pagination_loop_detected")
            seen_tokens.add(next_token)
            page_token = next_token

        raise FeishuProtocolError("feishu_pagination_limit_exceeded")

    async def aclose(self) -> None:
        """关闭底层有界 HTTP 连接池、释放 SDK 引用并清空缓存 token，可安全重复调用。"""
        async with self._client_lock:
            self._closed = True
            self._cached_token = None
            http_client = self._http_client
            self._http_client = None
            self._sdk_client = None
        if http_client is not None:
            await http_client.aclose()

    def _build_sdk_client(self) -> lark.Client:
        """构造启用显式异步 token 注入并压制日志级别的官方 lark-oapi 客户端。"""
        domain = self._config.base_url.rstrip("/")
        try:
            # 必须开启 enable_set_token(True) 且不向 SDK Config 写入 app_secret：
            # 1) 减少内存中明文密钥副本，app_secret 仅在异步换取 tenant_access_token 时发送；
            # 2) 从结构上杜绝意外跌落到 lark_oapi 同步阻塞的 TokenManager（requests.request）。
            # 日志级别压制为 ERROR，避免 SDK 默认 stdout Handler 输出多余或敏感诊断信息。
            return (
                lark.Client.builder()
                .app_id(self._config.app_id)
                .domain(domain)
                .timeout(self._config.timeout_seconds)
                .enable_set_token(True)
                .log_level(lark.LogLevel.ERROR)
                .build()
            )
        except ValueError:
            raise FeishuConfigurationError("feishu_base_url_invalid") from None

    def _build_http_client(self) -> httpx2.AsyncClient:
        """通过共享 HTTP 基座构造有界超时、有界连接池、禁重定向且仅声明 identity 的客户端。"""
        return build_bounded_async_client(
            timeout_seconds=self._config.timeout_seconds,
            transport=self._transport,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        )

    async def _ensure_runtime(self) -> tuple[lark.Client, httpx2.AsyncClient]:
        """在锁内原子获取或懒创建 SDK 与 HTTP 连接池，避免与并发 aclose() 竞态。"""
        async with self._client_lock:
            if self._closed:
                raise FeishuUpstreamError("feishu_client_closed")
            if self._sdk_client is None:
                self._sdk_client = self._build_sdk_client()
            if self._http_client is None:
                self._http_client = self._build_http_client()
            return self._sdk_client, self._http_client

    async def _invoke_tenant_sdk[RespT](
        self,
        call: Callable[[lark.Client, str], Awaitable[RespT]],
        *,
        operation: str,
        invalid_data_code: str = "feishu_response_invalid",
        invalid_item_code: str | None = None,
    ) -> tuple[RespT, dict[str, Any]]:
        """附带 tenant_access_token 调用 SDK；遇过期业务码时强制刷新 token 并重试一次。"""
        token = await self.get_tenant_access_token()
        try:
            return await self._invoke_sdk(
                lambda sdk: call(sdk, token),
                operation=operation,
                invalid_data_code=invalid_data_code,
                invalid_item_code=invalid_item_code,
            )
        except FeishuUpstreamError as error:
            if error.api_code in _FEISHU_TOKEN_EXPIRED_CODES:
                refreshed = await self.get_tenant_access_token(force_refresh=True)
                return await self._invoke_sdk(
                    lambda sdk: call(sdk, refreshed),
                    operation=operation,
                    invalid_data_code=invalid_data_code,
                    invalid_item_code=invalid_item_code,
                )
            raise

    async def _invoke_sdk[RespT](
        self,
        call: Callable[[lark.Client], Awaitable[RespT]],
        *,
        operation: str,
        invalid_data_code: str = "feishu_response_invalid",
        invalid_item_code: str | None = None,
    ) -> tuple[RespT, dict[str, Any]]:
        """在有界上下文与 tenacity 退避重试保护下执行一次官方 lark-oapi 异步调用。"""
        _ensure_lark_transport_bridge()
        sdk_client, http_client = await self._ensure_runtime()

        def _on_retry(attempt_number: int, delay: float, error: RetryableHttpError) -> None:
            if error.status_code is not None:
                logger.warning(
                    "feishu_upstream_retry operation=%s status_code=%d attempt=%d delay=%.2f",
                    operation,
                    error.status_code,
                    attempt_number,
                    delay,
                )
            else:
                logger.warning(
                    "feishu_timeout_retry operation=%s attempt=%d delay=%.2f",
                    operation,
                    attempt_number,
                    delay,
                )

        async def _sleep_if_open(seconds: float) -> None:
            if self._closed:
                raise FeishuUpstreamError("feishu_client_closed")
            await self._sleep(seconds)
            if self._closed:
                raise FeishuUpstreamError("feishu_client_closed")

        async def _attempt(_attempt_index: int) -> tuple[RespT, dict[str, Any]]:
            if self._closed:
                raise FeishuUpstreamError("feishu_client_closed")
            exec_ctx = _BoundedLarkExecutionContext(
                http_client=http_client,
                max_response_bytes=self._max_response_bytes,
                operation=operation,
            )
            ctx_token = _ACTIVE_LARK_CONTEXT.set(exec_ctx)
            try:
                response = await call(sdk_client)
            except asyncio.CancelledError:
                raise
            except FeishuError:
                raise
            except _FeishuHttpStatusSignal as signal:
                status_code = signal.status_code
                if status_code in _RETRYABLE_STATUS_CODES:
                    code = (
                        "feishu_rate_limited"
                        if status_code == 429
                        else "feishu_upstream_unavailable"
                    )
                    raise RetryableHttpError(
                        retry_after_header=signal.retry_after_header,
                        final_error=FeishuUpstreamError(
                            code,
                            retryable=True,
                            status_code=status_code,
                        ),
                        status_code=status_code,
                    ) from None
                # 非重试状态码（3xx 重定向、400/401/403 等）立即拒绝且绝不回显响应内容。
                logger.warning(
                    "feishu_upstream_failed operation=%s status_code=%d",
                    operation,
                    status_code,
                )
                raise FeishuUpstreamError(
                    "feishu_upstream_rejected",
                    retryable=False,
                    status_code=status_code,
                ) from None
            except httpx2.TimeoutException:
                # 截断底层 httpx2 异常链，防止请求体或 Authorization 头进入异常上下文。
                raise RetryableHttpError(
                    retry_after_header=None,
                    final_error=FeishuUpstreamError("feishu_timeout", retryable=True),
                    status_code=None,
                ) from None
            except httpx2.DecodingError as error:
                logger.warning(
                    "feishu_protocol_failed operation=%s exception_type=%s",
                    operation,
                    type(error).__name__,
                )
                raise FeishuProtocolError("feishu_response_decode_failed") from None
            except (httpx2.TransportError, httpx2.HTTPError, httpx2.StreamError) as error:
                if self._closed:
                    raise FeishuUpstreamError("feishu_client_closed") from None
                logger.warning(
                    "feishu_upstream_failed operation=%s code=feishu_transport_error "
                    "exception_type=%s",
                    operation,
                    type(error).__name__,
                )
                raise FeishuUpstreamError("feishu_transport_error", retryable=False) from None
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise FeishuProtocolError("feishu_response_invalid_json") from None
            except UnmarshalException as error:
                if (
                    invalid_item_code is not None
                    and error.field == "items"
                    and getattr(error.dst, "__origin__", None) is not list
                ):
                    raise FeishuProtocolError(invalid_item_code) from None
                raise FeishuProtocolError(invalid_data_code) from None
            except (
                AccessDeniedException,
                AccessTokenException,
                ClientAssertionException,
                InvalidArgsException,
                NoAuthorizationException,
                ObtainAccessTokenException,
            ) as error:
                logger.warning(
                    "feishu_upstream_failed operation=%s code=feishu_upstream_error "
                    "exception_type=%s",
                    operation,
                    type(error).__name__,
                )
                raise FeishuUpstreamError("feishu_upstream_error", retryable=False) from None
            except RuntimeError:
                if self._closed:
                    raise FeishuUpstreamError("feishu_client_closed") from None
                raise
            finally:
                _ACTIVE_LARK_CONTEXT.reset(ctx_token)

            if exec_ctx.last_payload is None:
                raise FeishuProtocolError("feishu_response_invalid")
            return response, exec_ctx.last_payload

        try:
            return await run_with_http_retry(
                _attempt,
                max_retries=self._max_retries,
                sleep=_sleep_if_open,
                base_seconds=_BASE_BACKOFF_SECONDS,
                max_seconds=_MAX_BACKOFF_SECONDS,
                on_retry=_on_retry,
            )
        except FeishuUpstreamError as error:
            if error.retryable:
                if error.status_code is not None:
                    logger.warning(
                        "feishu_upstream_failed operation=%s code=%s status_code=%d",
                        operation,
                        error.code,
                        error.status_code,
                    )
                else:
                    logger.warning(
                        "feishu_upstream_failed operation=%s code=%s",
                        operation,
                        error.code,
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


def _parse_remote_department(item: Department) -> RemoteDepartment:
    """将 lark-oapi Department 对象转换为领域 RemoteDepartment。"""
    external_id = _optional_text(item.open_department_id, max_length=128) or _optional_text(
        item.department_id,
        max_length=128,
    )
    name = _optional_text(item.name, max_length=120)
    if external_id is None or name is None:
        raise FeishuProtocolError("feishu_department_item_invalid")
    raw_parent = _optional_text(item.parent_department_id, max_length=128)
    parent_external_id = None if raw_parent in {None, "0"} else raw_parent

    raw_order: object = item.order if item.order is not None else 0
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

    status_obj = item.status
    is_deleted = bool(status_obj.is_deleted) if status_obj is not None else False

    return RemoteDepartment(
        external_id=external_id,
        parent_external_id=parent_external_id,
        name=name,
        order_index=order_index,
        is_deleted=is_deleted,
    )


def _parse_remote_employee(
    item: User,
    raw_item: Mapping[str, Any],
    queried_department_external_id: str,
) -> RemoteEmployee:
    """将 lark-oapi User 对象（辅以原始兼容字段）转换为领域 RemoteEmployee。"""
    union_id = _optional_text(item.union_id, max_length=128)
    open_id = _optional_text(item.open_id, max_length=128)
    if union_id is None and open_id is None:
        raise FeishuProtocolError("feishu_user_item_invalid")

    name = _optional_text(item.name, max_length=80) or _optional_text(
        item.en_name,
        max_length=80,
    )
    email = _optional_text(item.enterprise_email, max_length=320) or _optional_text(
        item.email,
        max_length=320,
    )
    employee_no = _optional_text(item.employee_no, max_length=64)
    job_title = _optional_text(item.job_title, max_length=120)

    avatar_url = _optional_text(raw_item.get("avatar_url"), max_length=1024)
    avatar_obj = item.avatar
    if avatar_url is None and avatar_obj is not None:
        avatar_url = (
            _optional_text(avatar_obj.avatar_origin, max_length=1024)
            or _optional_text(avatar_obj.avatar_240, max_length=1024)
            or _optional_text(avatar_obj.avatar_72, max_length=1024)
        )

    dept_ids: list[str] = []
    if isinstance(item.department_ids, list):
        for dept_id in item.department_ids:
            cleaned = _optional_text(dept_id, max_length=128)
            if cleaned is not None and cleaned != "0" and cleaned not in dept_ids:
                dept_ids.append(cleaned)
    if not dept_ids and queried_department_external_id != "0":
        dept_ids.append(queried_department_external_id)

    is_active = True
    if isinstance(raw_item.get("is_active"), bool):
        is_active = bool(raw_item["is_active"])
    if item.is_frozen is True:
        is_active = False
    status_obj = item.status
    if status_obj is not None and (
        bool(status_obj.is_resigned) or bool(status_obj.is_exited) or bool(status_obj.is_frozen)
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
    """检测 ASCII 及全部 Unicode Cc 控制字符（含 C0/DEL/C1），防止请求头注入或日志污染。"""
    if has_control_characters(value):
        return True
    if value.isascii():
        return False
    return any(unicodedata.category(ch) == "Cc" for ch in value)


def _validate_feishu_base_url(url: str, *, allow_http: bool) -> None:
    """校验飞书 Open API 域名前缀：与 lark_oapi._validate_domain 对齐，要求纯 origin。"""
    _validate_feishu_url(
        url,
        allow_http=allow_http,
        allow_query=False,
        code="feishu_base_url_invalid",
    )
    parsed = urlsplit(url.rstrip("/"))
    # 与 lark_oapi 域名安全策略在配置期提前对齐：禁止携带路径段，且测试用明文 HTTP 仅限回环地址。
    if parsed.path != "":
        raise FeishuConfigurationError("feishu_base_url_invalid")
    if parsed.scheme.lower() == "http" and (parsed.hostname or "").lower() not in _LOOPBACK_HOSTS:
        raise FeishuConfigurationError("feishu_base_url_invalid")


def _validate_feishu_url(
    url: str,
    *,
    allow_http: bool,
    allow_query: bool,
    code: str,
) -> None:
    """复用共享 HTTP URL 校验并叠加 Unicode Cc 控制字符拦截。"""
    if not url or _has_control_characters(url):
        raise FeishuConfigurationError(code)
    try:
        validate_http_base_url(url, allow_http=allow_http, allow_query=allow_query)
    except ValueError:
        raise FeishuConfigurationError(code) from None


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
