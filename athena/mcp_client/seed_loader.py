"""MCP Server seed loader.

On first startup (empty database), imports mcp_servers.yaml into the
mcp_servers table as a one-time initialization seed.
"""

from __future__ import annotations

import json

from athena.config import Config
from athena.logging_config import get_logger
from athena.models import get_session_maker

logger = get_logger(__name__)


async def seed_mcp_servers(config: Config) -> None:
    """Import mcp_servers.yaml as seed data if the database is empty.

    Only runs when no servers exist in the database. Existing servers
    are never overwritten.
    """
    if not config.mcp_servers_seed:
        logger.info("mcp_seed_empty_skip")
        return

    db_path = config.sqlite_db_path
    session_maker = get_session_maker(db_path)

    async with session_maker() as session:
        from sqlalchemy import select, func
        from athena.models.mcp_server import MCPServer

        # Check if any servers exist
        result = await session.execute(select(func.count()).select_from(MCPServer))
        count = result.scalar_one()

        if count > 0:
            logger.info("mcp_seed_skip_existing", count=count)
            return

        # Import seed servers
        for entry in config.mcp_servers_seed:
            server = MCPServer(
                server_id=entry["server_id"],
                name=entry["name"],
                transport=entry["transport"],
                connection_config=json.dumps(entry.get("connection_config", {})),
                enabled=True,
                source=entry.get("source", "external"),
            )
            session.add(server)

        await session.commit()
        logger.info("mcp_seed_imported", count=len(config.mcp_servers_seed))
