"""MCP 发现与执行共用的受控传输层工厂。"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from mcp.client._transport import Transport

from domain.mcp import McpConnectionInput, StdioConfig, StreamableHttpConfig
from infrastructure.mcp.http import DEFAULT_MAX_RESPONSE_BYTES, pinned_streamable_http_transport
from infrastructure.mcp.stdio import McpStdioTransport

__all__ = ["build_controlled_mcp_transport"]


def build_controlled_mcp_transport(
    connection: McpConnectionInput,
    *,
    allowed_private_hosts: frozenset[str] = frozenset(),
    maximum_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    stdio_base_environment: Mapping[str, str] | None = None,
    transport_factory: Callable[[McpConnectionInput], Transport] | None = None,
) -> Transport:
    """只通过受控 HTTP/stdio transport 建立连接，禁止绕过 SSRF 与子进程边界。"""
    if transport_factory is not None:
        return transport_factory(connection)
    config = connection.config
    if isinstance(config, StreamableHttpConfig):
        return pinned_streamable_http_transport(
            config.url,
            headers=dict(connection.secrets),
            allowed_private_hosts=allowed_private_hosts,
            maximum_response_bytes=maximum_response_bytes,
        )
    if isinstance(config, StdioConfig):
        return McpStdioTransport(
            config.command,
            config.args,
            cwd=config.cwd,
            env=connection.secrets,
            base_environment=stdio_base_environment,
            maximum_line_bytes=maximum_response_bytes,
            maximum_total_bytes=maximum_response_bytes,
        )
    raise ValueError("mcp_transport_unsupported")
