"""MCP Client package — unified tool-calling bridge.

Manages MCP server connections (stdio, HTTP, SSE) and provides a global
ToolRegistry for tool discovery, state management, and fallback resolution.
"""

from athena.mcp_client.client import MCPClient
from athena.mcp_client.registry import ToolRegistry
from athena.mcp_client.transports import (
    TransportFactory,
    StdioTransport,
    HttpTransport,
    SSETransport,
)

__all__ = [
    "MCPClient",
    "ToolRegistry",
    "TransportFactory",
    "StdioTransport",
    "HttpTransport",
    "SSETransport",
]
