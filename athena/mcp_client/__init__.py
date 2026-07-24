"""MCP Client package — unified tool-calling bridge.

Manages MCP server connections (stdio, HTTP, SSE) and provides a global
ToolRegistry for tool discovery, state management, and fallback resolution.
"""

from athena.mcp_client.client import MCPClient, RegisteredTool, get_mcp_client, set_mcp_client
from athena.mcp_client.transports import (
    TransportFactory,
    StdioTransport,
    HttpTransport,
    SSETransport,
)

__all__ = [
    "MCPClient",
    "RegisteredTool",
    "get_mcp_client",
    "set_mcp_client",
    "TransportFactory",
    "StdioTransport",
    "HttpTransport",
    "SSETransport",
]
