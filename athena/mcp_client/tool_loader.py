"""MCP Tool Loader — bridge between MCPClient and LangChain BaseTool.

Loads MCP tools as LangChain ``BaseTool`` objects by wrapping each
registered tool with a ``_MCPClientToolAdapter`` that delegates
``ainvoke()`` calls to ``MCPClient.call_tool()``.

This eliminates the previous ``MultiServerMCPClient`` dependency
(``langchain-mcp-adapters``) and ensures all tool calls flow through
the single ``MCPClient`` connection pool with its heartbeat, reconnect,
and state-management capabilities.

Caching: ``load_mcp_base_tools()`` caches the result in-process with a TTL.
Cache is invalidated automatically on expiry and manually via
``invalidate_tool_cache()`` (called by admin API on server add/remove/update).
"""

from __future__ import annotations

import time
from typing import Any

from langchain_core.tools import BaseTool

from athena.logging_config import get_logger

logger = get_logger(__name__)

# ── Module-level cache ───────────────────────────────────────────────
_cached_tools: list[BaseTool] | None = None
_cache_time: float = 0.0
CACHE_TTL_SECONDS = 300  # 5 minutes


def invalidate_tool_cache() -> None:
    """Invalidate the cached tools list.

    Called by admin API endpoints when MCP servers are
    registered, removed, or their status changes.
    """
    global _cached_tools, _cache_time
    _cached_tools = None
    _cache_time = 0.0
    logger.info("mcp_tool_cache_invalidated")


async def load_mcp_base_tools() -> list[BaseTool]:
    """Load all active MCP tools as LangChain ``BaseTool`` objects.

    Each tool is wrapped by ``_MCPClientToolAdapter`` which routes
    ``ainvoke()`` calls through the singleton ``MCPClient.call_tool()``.

    Uses an in-process cache with a 5-minute TTL.

    Returns:
        List of LangChain ``BaseTool`` objects. Empty list if no tools
        are registered or the MCPClient is unavailable.
    """
    global _cached_tools, _cache_time

    now = time.monotonic()
    if _cached_tools is not None and (now - _cache_time) < CACHE_TTL_SECONDS:
        logger.debug("mcp_tools_cache_hit", count=len(_cached_tools))
        return _cached_tools

    try:
        from athena.mcp_client.client import get_mcp_client

        mcp_client = get_mcp_client()
    except Exception as e:
        logger.warning("mcp_tools_load_dependency_failed", error=str(e))
        return []

    active_tools = mcp_client.get_active_tools()
    if not active_tools:
        logger.info("no_active_mcp_tools")
        return []

    tools: list[BaseTool] = []
    for tool in active_tools:
        adapter = _MCPClientToolAdapter(
            tool_name=tool.name,
            description=tool.description,
            parameters_schema=tool.parameters_schema,
            mcp_client=mcp_client,
            server_id=tool.source_server_id,
        )
        tools.append(adapter)

    _cached_tools = tools
    _cache_time = now
    logger.info("mcp_base_tools_loaded", count=len(tools))
    return tools


class _MCPClientToolAdapter(BaseTool):
    """Wraps ``MCPClient.call_tool()`` as a LangChain ``BaseTool``.

    Provides a unified interface so that both LLM tool-binding
    (``model.bind_tools()``) and tool execution (``ainvoke()``) use
    the same ``MCPClient`` connection pool, eliminating the previous
    dual-path via ``MultiServerMCPClient``.
    """

    mcp_client: Any  # MCPClient — avoid import cycle at class level
    server_id: str
    tool_name: str
    description: str
    parameters_schema: dict[str, Any]

    def _run(self, **kwargs: Any) -> str:
        raise NotImplementedError("Use async ainvoke() instead")

    async def _arun(self, **kwargs: Any) -> Any:
        result = await self.mcp_client.call_tool(
            server_id=self.server_id,
            tool_name=self.tool_name,
            arguments=kwargs,
        )
        if result.success:
            return result.content
        raise ToolCallError(result.error or "Tool returned failure")


class ToolCallError(Exception):
    """Raised when a MCP tool call returns a non-success result."""

    pass
