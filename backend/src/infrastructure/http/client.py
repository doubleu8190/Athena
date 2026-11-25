"""共享有界异步 HTTP 客户端（httpx2.AsyncClient）构造工厂。"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping

import httpx2

from infrastructure.http.bounded import BoundedResponseTransport

DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_CONNECTIONS = 32
DEFAULT_MAX_KEEPALIVE_CONNECTIONS = 16
DEFAULT_KEEPALIVE_EXPIRY_SECONDS = 30.0


def build_http_timeout(
    timeout_seconds: float,
    *,
    connect_timeout_seconds: float | None = None,
) -> httpx2.Timeout:
    """构造有界异步 HTTP 超时策略，默认将建连超时限制在 10 秒以内。"""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("http_timeout_invalid")
    connect = (
        min(DEFAULT_CONNECT_TIMEOUT_SECONDS, timeout_seconds)
        if connect_timeout_seconds is None
        else connect_timeout_seconds
    )
    if not math.isfinite(connect) or connect <= 0:
        raise ValueError("http_connect_timeout_invalid")
    return httpx2.Timeout(timeout=timeout_seconds, connect=connect)


def build_bounded_async_client(
    *,
    timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    connect_timeout_seconds: float | None = None,
    max_response_bytes: int | None = None,
    event_limit: int | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    headers: Mapping[str, str] | None = None,
    trust_env: bool = True,
    max_connections: int = DEFAULT_MAX_CONNECTIONS,
    max_keepalive_connections: int = DEFAULT_MAX_KEEPALIVE_CONNECTIONS,
    keepalive_expiry: float = DEFAULT_KEEPALIVE_EXPIRY_SECONDS,
    too_large_code: str = "llm_response_too_large",
    event_too_large_code: str = "llm_stream_event_too_large",
    encoding_unsupported_code: str = "llm_response_encoding_unsupported",
    length_invalid_code: str = "llm_response_length_invalid",
    reject_non_identity_encoding: bool = True,
    error_factory: Callable[[str], Exception] | None = None,
) -> httpx2.AsyncClient:
    """构造有界超时、有界连接池、固定禁用重定向及按需响应限流的异步客户端。"""
    if max_connections < 1 or max_keepalive_connections < 0:
        raise ValueError("http_limits_invalid")
    if not math.isfinite(keepalive_expiry) or keepalive_expiry < 0:
        raise ValueError("http_keepalive_expiry_invalid")

    timeout = build_http_timeout(
        timeout_seconds,
        connect_timeout_seconds=connect_timeout_seconds,
    )
    limits = httpx2.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
        keepalive_expiry=keepalive_expiry,
    )

    # 默认声明 Accept-Encoding: identity，避免透明压缩绕过原始响应字节上限检查。
    merged_headers: dict[str, str] = {}
    has_accept_encoding = False
    if headers is not None:
        for key, value in headers.items():
            merged_headers[key] = value
            if key.lower() == "accept-encoding":
                has_accept_encoding = True
    if not has_accept_encoding:
        merged_headers["Accept-Encoding"] = "identity"

    # 传入自定义 transport 时 httpx2 不会再为内层应用 limits，因此默认内层传输显式携带 limits。
    effective_transport: httpx2.AsyncBaseTransport | None = transport
    if max_response_bytes is not None:
        inner_transport = (
            transport
            if transport is not None
            else httpx2.AsyncHTTPTransport(limits=limits, trust_env=trust_env)
        )
        effective_transport = BoundedResponseTransport(
            inner_transport,
            max_response_bytes,
            event_limit=event_limit,
            too_large_code=too_large_code,
            event_too_large_code=event_too_large_code,
            encoding_unsupported_code=encoding_unsupported_code,
            length_invalid_code=length_invalid_code,
            reject_non_identity_encoding=reject_non_identity_encoding,
            error_factory=error_factory,
        )

    # 安全边界：受限公共 HTTP 客户端固定禁用重定向，防止认证头随 3xx 泄露或 SSRF 跳转。
    return httpx2.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=False,
        trust_env=trust_env,
        headers=merged_headers,
        transport=effective_transport,
    )


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_KEEPALIVE_EXPIRY_SECONDS",
    "DEFAULT_MAX_CONNECTIONS",
    "DEFAULT_MAX_KEEPALIVE_CONNECTIONS",
    "build_bounded_async_client",
    "build_http_timeout",
]
