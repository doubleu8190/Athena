"""MCP (Model Context Protocol) 工具集成.

提供 MCP 客户端连接管理、工具 Schema 转换、远程代理执行。
"""

from athena.core.tools.mcp.client import MCPClient
from athena.core.tools.mcp.adapter import MCPToolAdapter

__all__ = ["MCPClient", "MCPToolAdapter"]
