"""Built-in MCP server auto-registration.

On every startup, code-defined servers from mcp_servers.yaml are upserted
into the database.  User-created servers (source != 'builtin') are never
touched by this process.
"""

from __future__ import annotations

import json

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
