"""MCP Tool Loader — bridge between MultiServerMCPClient and LangChain BaseTool.

Loads MCP tools as LangChain ``BaseTool`` objects using
``MultiServerMCPClient`` from ``langchain-mcp-adapters``. These tools can be
used directly with ``model.bind_tools()`` for native function calling.

Caching: ``load_mcp_base_tools()`` caches the result in-process with a TTL.
Cache is invalidated automatically on expiry and manually via
``invalidate_tool_cache()`` (called by admin API on server add/remove/update).
"""

from __future__ import annotations

import json
import time
from typing import Any

from langchain_core.tools import BaseTool

from athena.logging_config import get_logger

logger = get_logger(__name__)

# ── Module-level cache ───────────────────────────────────────────────
_cached_tools: list[BaseTool] | None = None
_cached_configs: dict[str, dict[str, Any]] | None = None
_cache_time: float = 0.0
CACHE_TTL_SECONDS = 300  # 5 minutes


def invalidate_tool_cache() -> None:
    """Invalidate the cached tools list.

    Called by admin API endpoints when MCP servers are
    registered, removed, or their status changes.
    """
    global _cached_tools, _cached_configs, _cache_time
    _cached_tools = None
    _cached_configs = None
    _cache_time = 0.0
    logger.info("mcp_tool_cache_invalidated")


async def load_mcp_base_tools() -> list[BaseTool]:
    """Load all enabled MCP tools as LangChain ``BaseTool`` objects.

    Uses an in-process cache with a 5-minute TTL. Each cache miss
    creates a fresh ``MultiServerMCPClient`` and fetches the latest
    tool list.

    The returned ``BaseTool`` objects create a new MCP session per
    ``ainvoke()`` call, so no long-lived context manager is needed.

    Returns:
        List of LangChain ``BaseTool`` objects. Empty list if no servers
        are configured or loading fails.
    """
    global _cached_tools, _cached_configs, _cache_time

    now = time.monotonic()
    if _cached_tools is not None and (now - _cache_time) < CACHE_TTL_SECONDS:
        logger.debug("mcp_tools_cache_hit", count=len(_cached_tools))
        return _cached_tools

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logger.warning("langchain_mcp_adapters_not_installed")
        return []

    server_configs = await _build_server_configs()
    logger.info("mcp_server_configs_built", server_configs=server_configs)
    if not server_configs:
        logger.info("no_mcp_servers_configured")
        return []

    try:
        client = MultiServerMCPClient(server_configs)
        tools = await client.get_tools()
        logger.info("mcp_base_tools_loaded", count=len(tools))

        _cached_tools = tools
        _cached_configs = server_configs
        _cache_time = now

        return tools
    except Exception as e:
        logger.error("mcp_base_tools_load_failed", error=str(e))
        return []


async def _build_server_configs() -> dict[str, dict[str, Any]]:
    """Build MultiServerMCPClient connection configs from the database.

    Reads enabled MCP servers from the DB and converts them to the
    ``MultiServerMCPClient`` connection format:

    .. code-block:: python

        {
            "server_id": {
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "athena.tools.server"],
            }
        }

    Results are cached per-process with a 5-minute TTL.
    """
    global _cached_configs, _cache_time

    now = time.monotonic()
    if _cached_configs is not None and (now - _cache_time) < CACHE_TTL_SECONDS:
        logger.debug("mcp_server_configs_cache_hit")
        return _cached_configs

    from athena.config import get_config
    from athena.models import get_session_maker
    from athena.models.mcp_server import MCPServer
    from sqlalchemy import select

    config = get_config()
    session_maker = get_session_maker(config.sqlite_db_path)

    async with session_maker() as session:
        result = await session.execute(
            select(MCPServer).where(MCPServer.enabled == True)  # noqa: E712
        )
        servers = result.scalars().all()

    configs: dict[str, dict[str, Any]] = {}
    for server in servers:
        try:
            conn_config = json.loads(server.connection_config)
        except json.JSONDecodeError:
            logger.warning(
                "mcp_server_invalid_config",
                server_id=server.server_id,
            )
            continue

        transport = server.transport
        entry: dict[str, Any] = {"transport": transport}

        if transport == "stdio":
            entry["command"] = conn_config.get("command", "")
            if "args" in conn_config:
                entry["args"] = conn_config["args"]
            if "env" in conn_config:
                entry["env"] = conn_config["env"]
        elif transport in ("sse", "http", "streamable_http"):
            entry["url"] = conn_config.get("url", "")
            if "headers" in conn_config:
                entry["headers"] = conn_config["headers"]
        else:
            logger.warning(
                "mcp_server_unsupported_transport",
                server_id=server.server_id,
                transport=transport,
            )
            continue

        configs[server.server_id] = entry

    _cached_configs = configs
    _cache_time = now
    return configs
