"""面向超管受控内网场景的 Streamable HTTP MCP 传输层（基于标准 httpx2 与官方 MCP SDK）。"""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx2
from mcp.client.streamable_http import streamable_http_client

from infrastructure.http import has_control_characters

DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
_CLOUD_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("169.254.170.2"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


class McpEndpointError(ValueError):
    """MCP endpoint 不符合网络访问策略。"""


class McpAccessDeniedError(ConnectionError):
    """远端拒绝认证；该错误不会触发 MCP 协议回退。"""


class McpRedirectRejectedError(ConnectionError):
    """MCP endpoint 返回了被禁止的重定向。"""


@dataclass(frozen=True, slots=True)
class McpEndpoint:
    """已验证的 MCP HTTPS endpoint。"""

    url: httpx2.URL
    hostname: str
    port: int


def _is_cloud_metadata_literal(hostname: str) -> bool:
    """检查字面主机名是否为云平台元数据 IP（含 IPv4-mapped IPv6 形式）。"""
    try:
        address = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        return False
    if address in _CLOUD_METADATA_IPS:
        return True
    return isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped in _CLOUD_METADATA_IPS


def validate_mcp_endpoint(url: str) -> McpEndpoint:
    """校验 HTTPS endpoint，拒绝 URL 凭据、查询串、片段、控制字符与字面云元数据 IP。"""
    if not url or has_control_characters(url):
        raise McpEndpointError("mcp_endpoint_invalid")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "@" in parsed.netloc
            or parsed.query
            or "?" in url
            or parsed.fragment
            or "#" in url
            or _is_cloud_metadata_literal(parsed.hostname)
        ):
            raise McpEndpointError("mcp_endpoint_invalid")
        return McpEndpoint(
            httpx2.URL(url),
            parsed.hostname.encode("idna").decode("ascii"),
            parsed.port or 443,
        )
    except (UnicodeError, ValueError) as error:
        if isinstance(error, McpEndpointError):
            raise
        raise McpEndpointError("mcp_endpoint_invalid") from error


async def _check_mcp_response(response: httpx2.Response) -> None:
    """在 SDK 跟随同源重定向或协议回退前，统一拦截 3xx 与 401/403 响应。"""
    if 300 <= response.status_code < 400:
        await response.aclose()
        raise McpRedirectRejectedError("mcp_redirect_rejected")
    if response.status_code in {401, 403}:
        await response.aclose()
        raise McpAccessDeniedError("mcp_access_denied")


def build_mcp_http_client(
    endpoint: str,
    *,
    headers: Mapping[str, str] | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    timeout: httpx2.Timeout | None = None,
) -> httpx2.AsyncClient:
    """构造单次调用独立的无 Cookie、禁重定向且校验响应状态的 MCP HTTP 客户端。"""
    validate_mcp_endpoint(endpoint)
    return httpx2.AsyncClient(
        headers=dict(headers) if headers is not None else None,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
        timeout=timeout or httpx2.Timeout(DEFAULT_HTTP_TIMEOUT_SECONDS, read=30.0),
        event_hooks={"response": [_check_mcp_response]},
    )


@asynccontextmanager
async def mcp_streamable_http_transport(
    endpoint: str,
    *,
    headers: Mapping[str, str] | None = None,
    maximum_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    transport: httpx2.AsyncBaseTransport | None = None,
    timeout: httpx2.Timeout | None = None,
) -> AsyncGenerator[Any, None]:
    """创建自动释放客户端且设置 SSE 单事件上限的官方 Streamable HTTP transport。"""
    if maximum_response_bytes < 1:
        raise ValueError("maximum_response_bytes_invalid")
    async with build_mcp_http_client(
        endpoint,
        headers=headers,
        transport=transport,
        timeout=timeout,
    ) as client:
        async with streamable_http_client(
            endpoint,
            http_client=client,
            max_sse_event_size=maximum_response_bytes,
        ) as streams:
            yield streams


__all__ = [
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "McpAccessDeniedError",
    "McpEndpoint",
    "McpEndpointError",
    "McpRedirectRejectedError",
    "build_mcp_http_client",
    "mcp_streamable_http_transport",
    "validate_mcp_endpoint",
]
