"""Lazy Neo4j resource for the target bootstrap lifecycle."""

from __future__ import annotations

from .adapter import Neo4jGraphIndex
from .options import Neo4jOptions


class Neo4jResource:
    def __init__(self, options: Neo4jOptions | object) -> None:
        self.options = options if isinstance(options, Neo4jOptions) else Neo4jOptions(
            uri=options.neo4j_uri,
            username=options.neo4j_username,
            password=options.neo4j_password,
            database=options.neo4j_database,
            max_connection_pool_size=options.neo4j_max_connection_pool_size,
            connection_timeout_seconds=options.neo4j_connection_timeout_seconds,
        )
        self.adapter: Neo4jGraphIndex | None = None

    async def start(self) -> None:
        if self.adapter is not None:
            return
        from neo4j import AsyncGraphDatabase

        driver = AsyncGraphDatabase.driver(
            self.options.uri,
            auth=(self.options.username, self.options.password),
            max_connection_pool_size=self.options.max_connection_pool_size,
            connection_timeout=self.options.connection_timeout_seconds,
        )
        self.adapter = Neo4jGraphIndex(driver, database=self.options.database)

    async def stop(self) -> None:
        if self.adapter is not None:
            await self.adapter.close()
        self.adapter = None

    async def health(self) -> bool:
        return self.adapter is not None and await self.adapter.health()

    async def query_context(
        self, query: str, *, limit: int = 12
    ) -> list[dict[str, object]]:
        """Expose graph retrieval as a capability-level port."""
        if self.adapter is None:
            return []
        return await self.adapter.query_context(query, limit=limit)

    async def search_nodes(self, query: str, *, limit: int = 12) -> list[dict[str, object]]:
        return await self.query_context(query, limit=limit)


__all__ = ["Neo4jResource"]
