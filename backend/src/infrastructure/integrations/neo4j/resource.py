"""Lazy Neo4j resource for the target bootstrap lifecycle."""

from __future__ import annotations

from .adapter import Neo4jGraphAdapter


class Neo4jResource:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.adapter: Neo4jGraphAdapter | None = None

    async def start(self) -> None:
        if self.adapter is not None:
            return
        from neo4j import AsyncGraphDatabase

        driver = AsyncGraphDatabase.driver(
            self.settings.neo4j_uri,
            auth=(self.settings.neo4j_username, self.settings.neo4j_password),
            max_connection_pool_size=self.settings.neo4j_max_connection_pool_size,
            connection_timeout=self.settings.neo4j_connection_timeout_seconds,
        )
        self.adapter = Neo4jGraphAdapter(driver, database=self.settings.neo4j_database)

    async def stop(self) -> None:
        if self.adapter is not None:
            await self.adapter.close()
        self.adapter = None

    async def health(self) -> bool:
        return self.adapter is not None and await self.adapter.health()


__all__ = ["Neo4jResource"]
