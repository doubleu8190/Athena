"""受控 Streamable HTTP transport。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit

import httpx2
from httpx2 import AsyncByteStream, AsyncHTTPTransport, Request, Response
from mcp.client._transport import TransportStreams
from mcp.client.streamable_http import streamable_http_client

from infrastructure.http.bounded import BoundedResponseStream

DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("fd00:ec2::254"),
}
_IPV6_NAT64_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)
_IPV6_SPECIAL_PREFIXES = (
    ipaddress.ip_network("2001::/23"),
    ipaddress.ip_network("2001:db8::/32"),
    ipaddress.ip_network("3fff::/20"),
)
_ALLOWLISTABLE_PRIVATE_PREFIXES = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


class McpEndpointError(ValueError):
    """MCP endpoint 不符合网络访问策略。"""


class McpAccessDeniedError(ConnectionError):
    """远端拒绝认证；该错误不会触发 MCP 协议回退。"""


class McpRedirectRejectedError(ConnectionError):
    """MCP endpoint 返回了被禁止的重定向。"""


class McpResponseTooLargeError(ConnectionError):
    """MCP HTTP 响应超过字节上限。"""


Resolver = Callable[[str, int], Awaitable[Iterable[str]]]


@dataclass(frozen=True, slots=True)
class McpEndpoint:
    """已验证的 MCP HTTPS endpoint。"""

    url: httpx2.URL
    hostname: str
    port: int


def validate_mcp_endpoint(url: str) -> McpEndpoint:
    """校验 HTTPS endpoint 并拒绝可携带凭据或额外目标信息的 URL。"""
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise McpEndpointError("mcp_endpoint_invalid")
        port = parsed.port or 443
        endpoint = httpx2.URL(url)
        return McpEndpoint(endpoint, parsed.hostname.encode("idna").decode("ascii"), port)
    except (UnicodeError, ValueError) as error:
        if isinstance(error, McpEndpointError):
            raise
        raise McpEndpointError("mcp_endpoint_invalid") from error


def _is_allowed_address(value: str, *, allow_private: bool) -> bool:
    if "%" in value:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address in _METADATA_IPS:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if (
            address.ipv4_mapped is not None
            or address.sixtofour is not None
            or address.teredo is not None
            or any(address in prefix for prefix in _IPV6_NAT64_PREFIXES)
            or any(address in prefix for prefix in _IPV6_SPECIAL_PREFIXES)
        ):
            return False
    if (
        address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    if address.is_global:
        return True
    if allow_private and address.is_loopback:
        return True
    return allow_private and any(
        address.version == prefix.version and address in prefix
        for prefix in _ALLOWLISTABLE_PRIVATE_PREFIXES
    )


async def _resolve(hostname: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(
        hostname,
        port,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return tuple(dict.fromkeys(record[4][0] for record in records))


class PinnedHttpTransport(httpx2.AsyncBaseTransport):
    """拒绝私网目标和重定向，并将请求固定到已检查的 DNS 地址。"""

    def __init__(
        self,
        endpoint: str,
        *,
        allowed_private_hosts: frozenset[str] = frozenset(),
        maximum_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        resolver: Resolver | None = None,
        inner_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        """保存 endpoint 策略与可替换的 DNS/HTTP 依赖。"""
        if maximum_response_bytes < 1:
            raise ValueError("maximum_response_bytes_invalid")
        self._endpoint = validate_mcp_endpoint(endpoint)
        self._allowed_private_hosts = frozenset(
            host.encode("idna").decode("ascii").lower() for host in allowed_private_hosts
        )
        self._maximum_response_bytes = maximum_response_bytes
        self._resolver = resolver or _resolve
        self._transport = inner_transport or AsyncHTTPTransport(trust_env=False, retries=0)
        self._addresses: tuple[str, ...] | None = None
        self._resolve_lock = asyncio.Lock()

    async def handle_async_request(self, request: Request) -> Response:
        """用 endpoint 的固定解析地址发送一次 HTTP 请求。"""
        self._validate_request_url(request.url)
        addresses = await self._checked_addresses()
        address = addresses[0]
        original_url = request.url
        rewritten_url = original_url.copy_with(host=address)
        headers = httpx2.Headers(request.headers)
        headers["accept-encoding"] = "identity"
        host_value = self._endpoint.hostname
        if ":" in host_value:
            host_value = f"[{host_value}]"
        if self._endpoint.port != 443:
            host_value = f"{host_value}:{self._endpoint.port}"
        headers["host"] = host_value
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = self._endpoint.hostname
        pinned_request = Request(
            request.method,
            rewritten_url,
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        response = await self._transport.handle_async_request(pinned_request)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise McpRedirectRejectedError("mcp_redirect_rejected")
        if response.status_code in {401, 403}:
            await response.aclose()
            raise McpAccessDeniedError("mcp_access_denied")
        # 只接受缺省编码或 identity；空值和压缩编码一律拒绝。
        if "content-encoding" in response.headers:
            content_encoding = response.headers["content-encoding"].strip().lower()
            if content_encoding != "identity":
                await response.aclose()
                raise McpEndpointError("mcp_content_encoding_rejected")
        declared_length = response.headers.get("content-length")
        if declared_length is not None:
            try:
                if int(declared_length) > self._maximum_response_bytes:
                    await response.aclose()
                    raise McpResponseTooLargeError("mcp_response_too_large")
            except ValueError:
                await response.aclose()
                raise McpEndpointError("mcp_response_invalid") from None
        # 复用共享有界流包装器在读取阶段计数字节，超限时抛出 MCP 专用异常。
        response.stream = BoundedResponseStream(
            cast(AsyncByteStream, response.stream),
            self._maximum_response_bytes,
            is_sse=False,
            too_large_code="mcp_response_too_large",
            error_factory=McpResponseTooLargeError,
        )
        response.request = request
        return response

    async def aclose(self) -> None:
        """释放底层 HTTP 连接池。"""
        await self._transport.aclose()

    def _validate_request_url(self, url: httpx2.URL) -> None:
        if (
            url.scheme != "https"
            or url.host.lower() != self._endpoint.hostname.lower()
            or (url.port or 443) != self._endpoint.port
            or url.path != self._endpoint.url.path
            or url.query
            or url.fragment
            or url.userinfo
        ):
            raise McpEndpointError("mcp_endpoint_changed")

    async def _checked_addresses(self) -> tuple[str, ...]:
        if self._addresses is not None:
            return self._addresses
        async with self._resolve_lock:
            if self._addresses is not None:
                return self._addresses
            resolved = await self._resolver(self._endpoint.hostname, self._endpoint.port)
            if isinstance(resolved, (str, bytes)):
                raise McpEndpointError("mcp_dns_invalid")
            if not isinstance(resolved, Iterable):
                raise McpEndpointError("mcp_dns_invalid")
            addresses = tuple(dict.fromkeys(resolved))
            if not addresses:
                raise McpEndpointError("mcp_dns_empty")
            allow_private = self._endpoint.hostname.lower() in self._allowed_private_hosts
            if any(
                not _is_allowed_address(address, allow_private=allow_private)
                for address in addresses
            ):
                raise McpEndpointError("mcp_dns_address_rejected")
            self._addresses = addresses
            return addresses


def build_pinned_http_client(
    endpoint: str,
    *,
    headers: dict[str, str] | None = None,
    allowed_private_hosts: frozenset[str] = frozenset(),
    maximum_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    resolver: Resolver | None = None,
    inner_transport: httpx2.AsyncBaseTransport | None = None,
) -> httpx2.AsyncClient:
    """创建供 SDK Streamable HTTP transport 使用的单次请求客户端。"""
    transport = PinnedHttpTransport(
        endpoint,
        allowed_private_hosts=allowed_private_hosts,
        maximum_response_bytes=maximum_response_bytes,
        resolver=resolver,
        inner_transport=inner_transport,
    )
    return httpx2.AsyncClient(
        headers=headers,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
        timeout=httpx2.Timeout(30.0, read=30.0),
    )


@asynccontextmanager
async def pinned_streamable_http_transport(
    endpoint: str,
    *,
    headers: dict[str, str] | None = None,
    allowed_private_hosts: frozenset[str] = frozenset(),
    maximum_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    resolver: Resolver | None = None,
    inner_transport: httpx2.AsyncBaseTransport | None = None,
) -> AsyncGenerator[TransportStreams, None]:
    """创建自动关闭资源且设置响应、SSE 上限的 SDK Streamable HTTP transport。"""
    async with build_pinned_http_client(
        endpoint,
        headers=headers,
        allowed_private_hosts=allowed_private_hosts,
        maximum_response_bytes=maximum_response_bytes,
        resolver=resolver,
        inner_transport=inner_transport,
    ) as client:
        async with streamable_http_client(
            endpoint,
            http_client=client,
            max_sse_event_size=maximum_response_bytes,
        ) as streams:
            yield streams


__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "McpAccessDeniedError",
    "McpEndpoint",
    "McpEndpointError",
    "McpRedirectRejectedError",
    "McpResponseTooLargeError",
    "PinnedHttpTransport",
    "build_pinned_http_client",
    "pinned_streamable_http_transport",
    "validate_mcp_endpoint",
]
