"""Driver-injected graph index adapter."""

from __future__ import annotations

from domain.files import GraphIndexerPort
from domain.retrieval import GraphSearchPort

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

    async def index_attachment(self, attachment, chunks) -> None:
        async with self._driver.session(database=self._database) as session:
            for chunk in chunks:
                await session.run(
                    "MERGE (c:FileChunk {id: $id}) SET c.attachment_id = $attachment_id, c.content = $content",
                    id=chunk.id,
                    attachment_id=attachment.id,
                    content=chunk.content,
                )

    async def delete_attachment(self, attachment_id: str) -> None:
        async with self._driver.session(database=self._database) as session:
            await session.run("MATCH (c:FileChunk {attachment_id: $attachment_id}) DETACH DELETE c", attachment_id=attachment_id)

    async def query_context(self, query: str, *, limit: int = 12) -> list[dict[str, object]]:
        """按内容匹配返回图节点摘要。"""
        if not query.strip():
            return []
        async with self._driver.session(database=self._database) as session:
            result = await session.run(
                "MATCH (c:FileChunk) WHERE toLower(c.content) CONTAINS toLower($query) "
                "RETURN c.id AS id, c.attachment_id AS attachment_id, c.content AS content LIMIT $limit",
                query=query.strip(), limit=max(0, limit),
            )
            rows = []
            async for record in result:
                rows.append(dict(record))
            return rows

    async def search_nodes(self, query: str, *, limit: int = 12) -> list[dict[str, object]]:
        return await self.query_context(query, limit=limit)


Neo4jGraphAdapter = Neo4jGraphIndex

__all__ = ["Neo4jGraphIndex", "Neo4jGraphAdapter"]
