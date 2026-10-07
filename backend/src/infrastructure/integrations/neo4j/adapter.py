"""Driver-injected graph index adapter."""

from __future__ import annotations


class Neo4jGraphAdapter:
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


__all__ = ["Neo4jGraphAdapter"]
