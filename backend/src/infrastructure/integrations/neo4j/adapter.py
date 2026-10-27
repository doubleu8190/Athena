"""Driver-injected graph index adapter."""

from __future__ import annotations

from domain.files import GraphIndexerPort
from domain.retrieval import GraphSearchPort
from domain.leases import LeaseGrant

class Neo4jGraphIndex(GraphIndexerPort, GraphSearchPort):
    """Keep Neo4j driver details outside domain and application layers."""

    def __init__(self, driver, *, database: str = "neo4j") -> None:
        self._driver = driver
        self._database = database

    async def health(self) -> bool:
        try:
            async with self._driver.session(database=self._database) as session:
                result = await session.run("RETURN 1 AS health")
                record = await result.single()
                return record is not None and record["health"] == 1
        except Exception:
            return False

    async def close(self) -> None:
        await self._driver.close()

    async def index_attachment(self, attachment, chunks, *, lease: LeaseGrant | None = None) -> None:
        async with self._driver.session(database=self._database) as session:
            for chunk in chunks:
                await session.run(
                    "MERGE (c:FileChunk {id: $id}) SET c.attachment_id = $attachment_id, c.index_revision_id = $revision_id, c.content = $content",
                    id=chunk.id,
                    attachment_id=attachment.id,
                    revision_id=chunk.index_revision_id,
                    content=chunk.content,
                )

    async def delete_attachment(self, attachment_id: str) -> None:
        async with self._driver.session(database=self._database) as session:
            await session.run("MATCH (c:FileChunk {attachment_id: $attachment_id}) DETACH DELETE c", attachment_id=attachment_id)

    async def query_context(
        self, query: str, *, limit: int = 12, after_id: str | None = None
    ) -> list[dict[str, object]]:
        """按内容匹配返回图节点摘要。"""
        if not query.strip() or limit <= 0:
            return []
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (c:FileChunk) WHERE toLower(c.content) CONTAINS toLower($query) "
                "AND ($after_id IS NULL OR c.id > $after_id) "
                "RETURN c.id AS id, c.attachment_id AS attachment_id, "
                "c.index_revision_id AS index_revision_id, "
                "c.index_revision_id AS revision_id, c.content AS content "
                "ORDER BY c.id LIMIT $limit",
                parameters={"query": query.strip(), "after_id": after_id, "limit": limit},
            )
            rows = []
            async for record in result:
                rows.append(dict(record))
            return rows

    async def search_nodes(
        self, query: str, *, limit: int = 12, after_id: str | None = None
    ) -> list[dict[str, object]]:
        return await self.query_context(query, limit=limit, after_id=after_id)


Neo4jGraphAdapter = Neo4jGraphIndex

__all__ = ["Neo4jGraphIndex", "Neo4jGraphAdapter"]
