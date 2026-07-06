"""MCP Tool Loader — bridge between MultiServerMCPClient and LangChain BaseTool.

Loads MCP tools as LangChain ``BaseTool`` objects using
``MultiServerMCPClient`` from ``langchain-mcp-adapters``. These tools can be
used directly with ``model.bind_tools()`` for native function calling.

Each call to ``load_mcp_base_tools()`` creates a fresh ``MultiServerMCPClient``
and fetches the latest tool list, supporting hot-plugging of MCP servers.
The ``BaseTool.ainvoke()`` on returned tools creates a new session per call,
so no long-lived client context is needed.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool

from athena.logging_config import get_logger

logger = get_logger(__name__)


async def load_mcp_base_tools() -> list[BaseTool]:
    """Load all enabled MCP tools as LangChain ``BaseTool`` objects.

    Uses ``MultiServerMCPClient`` to connect to all enabled MCP servers
    and convert their tools to LangChain's ``BaseTool`` format, suitable
    for ``model.bind_tools()``.

    Each call creates a fresh client and fetches the latest tool list,
    so newly added/removed MCP servers are picked up automatically.

    The returned ``BaseTool`` objects create a new MCP session per
    ``ainvoke()`` call, so no long-lived context manager is needed.

    Returns:
        List of LangChain ``BaseTool`` objects. Empty list if no servers
        are configured or loading fails.
    """
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

    Returns:
        Dict mapping server_id to connection config.
    """
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

    return configs
