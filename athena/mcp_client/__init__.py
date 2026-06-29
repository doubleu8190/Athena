"""MCP Client package — unified tool-calling bridge.

Manages MCP server connections (stdio, HTTP, SSE) and provides a global
ToolRegistry for tool discovery, state management, and fallback resolution.
"""

from athena.mcp_client.client import MCPClient, get_mcp_client, set_mcp_client
from athena.mcp_client.registry import ToolRegistry, get_tool_registry
from athena.mcp_client.transports import (
    TransportFactory,
    StdioTransport,
    HttpTransport,
    SSETransport,
)

__all__ = [
    "MCPClient",
    "get_mcp_client",
    "set_mcp_client",
    "ToolRegistry",
    "get_tool_registry",
    "TransportFactory",
    "StdioTransport",
    "HttpTransport",
    "SSETransport",
]
