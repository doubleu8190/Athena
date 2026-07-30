"""Built-in MCP server auto-registration.

On every startup, code-defined servers from mcp_servers.yaml are upserted
into the database.  User-created servers (source != 'builtin') are never
touched by this process.

Also registers native tools (file operations, weather) that bypass the
MCP transport layer for low-latency execution.
"""

from __future__ import annotations

import json
from typing import Any

from athena.config import Config
from athena.logging_config import get_logger
from athena.models import get_session_maker

logger = get_logger(__name__)


async def auto_register_builtin_servers(config: Config) -> None:
    """Upsert code-defined MCP servers into the database on every startup.

    For each server defined in the YAML seed:
    - server_id not in DB → INSERT with enabled=True
    - server_id exists and source == 'builtin' → UPDATE name, transport,
      connection_config (but NOT enabled — respect user's explicit disable)
    - server_id exists and source != 'builtin' → SKIP (user-created)
    """
    if not config.mcp_servers_seed:
        logger.info("mcp_auto_register_empty_skip")
        return

    db_path = config.sqlite_db_path
    session_maker = get_session_maker(db_path)

    async with session_maker() as session:
        from athena.models.mcp_server import MCPServer

        inserted = 0
        updated = 0
        skipped = 0

        for entry in config.mcp_servers_seed:
            server_id = entry["server_id"]
            existing = await session.get(MCPServer, server_id)

            if existing is None:
                # New built-in server — insert
                server = MCPServer(
                    server_id=server_id,
                    name=entry["name"],
                    transport=entry["transport"],
                    connection_config=json.dumps(entry.get("connection_config", {})),
                    enabled=True,
                    source="builtin",
                )
                session.add(server)
                inserted += 1
                logger.info("mcp_auto_register_insert", server_id=server_id)
            elif existing.source == "builtin":
                # Existing built-in — update config from code (but preserve enabled)
                existing.name = entry["name"]
                existing.transport = entry["transport"]
                existing.connection_config = json.dumps(
                    entry.get("connection_config", {})
                )
                updated += 1
                logger.info("mcp_auto_register_update", server_id=server_id)
            else:
                # User-created server — leave untouched
                skipped += 1
                logger.info(
                    "mcp_auto_register_skip_user",
                    server_id=server_id,
                    source=existing.source,
                )

        await session.commit()

        logger.info(
            "mcp_auto_register_done",
            total=len(config.mcp_servers_seed),
            inserted=inserted,
            updated=updated,
            skipped=skipped,
        )


# ── Native Tool Registration ─────────────────────────────────────


async def register_native_tools(mcp_client: Any) -> None:
    """Register native (direct-call) tools with the MCPClient.

    Native tools bypass the MCP transport layer entirely. They are
    appropriate for high-frequency, lightweight operations like file
    I/O and weather queries where the overhead of JSON-RPC + subprocess
    spawning is unnecessary.

    Tools registered as native:
    - file_read, file_write, file_search, file_delete
    - query_weather
    """
    registered = 0

    # ── File operation tools ──────────────────────────────────────
    try:
        from athena.tools.filesystem import file_read, file_write, file_search, file_delete

        mcp_client.register_native_tool(
            name="file_read",
            handler=_make_native_handler(file_read),
            description="Read a file from the workspace. Returns file content as text.",
            risk_level="low",
            capability_tags=["filesystem", "read"],
        )
        registered += 1

        mcp_client.register_native_tool(
            name="file_write",
            handler=_make_native_handler(file_write),
            description="Write content to a file in the workspace. Creates parent directories if needed.",
            risk_level="medium",
            capability_tags=["filesystem", "write"],
        )
        registered += 1

        mcp_client.register_native_tool(
            name="file_search",
            handler=_make_native_handler(file_search),
            description="Search for files under a directory. Supports name glob and content search.",
            risk_level="low",
            capability_tags=["filesystem", "search"],
        )
        registered += 1

        mcp_client.register_native_tool(
            name="file_delete",
            handler=_make_native_handler(file_delete),
            description="Delete a file or directory from the workspace.",
            risk_level="high",
            capability_tags=["filesystem", "delete"],
        )
        registered += 1
    except ImportError as e:
        logger.warning("native_tools_filesystem_import_failed", error=str(e))

    # ── Weather tool ──────────────────────────────────────────────
    try:
        from athena.tools.weather import query_weather

        mcp_client.register_native_tool(
            name="query_weather",
            handler=_make_native_handler(query_weather),
            description="Query weather for a city. Supports Chinese and English city names.",
            risk_level="low",
            capability_tags=["weather", "external"],
        )
        registered += 1
    except ImportError as e:
        logger.warning("native_tools_weather_import_failed", error=str(e))

    logger.info("native_tools_registered", count=registered)


def _make_native_handler(func: Any) -> Any:
    """Wrap an async tool function to return string results for the LLM.

    The native handler adapter:
    1. Calls the original async function with kwargs
    2. Serializes the result to a JSON string or plain text
    3. Handles errors gracefully
    """
    async def handler(**kwargs: Any) -> str:
        try:
            result = await func(**kwargs)
            if hasattr(result, "to_dict"):
                return json.dumps(result.to_dict(), ensure_ascii=False)
            if hasattr(result, "success") and not result.success:
                error_msg = getattr(result, "error", str(result))
                return json.dumps({"error": error_msg}, ensure_ascii=False)
            if isinstance(result, str):
                return result
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            logger.error("native_tool_handler_error", func=getattr(func, '__name__', str(func)), error=str(e))
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    handler.__name__ = f"native_{getattr(func, '__name__', 'unknown')}"
    return handler
