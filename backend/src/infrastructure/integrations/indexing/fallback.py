"""Explicit no-op indexers for deployments without external indexes."""

from domain.files import Attachment, FileChunk, GraphIndexerPort, VectorIndexerPort


class DisabledVectorIndexer(VectorIndexerPort):
    """Ignore vector indexing when embeddings are disabled."""

    async def index_attachment(self, attachment: Attachment, chunks: list[FileChunk]) -> None:
        return None

    async def delete_attachment(self, attachment_id: str) -> None:
        return None

    async def index(self, attachment: Attachment, chunks: list[FileChunk]) -> None:
        return await self.index_attachment(attachment, chunks)

    async def delete(self, attachment_id: str) -> None:
        return await self.delete_attachment(attachment_id)


class DisabledGraphIndexer(GraphIndexerPort):
    """Ignore graph indexing when Neo4j is disabled."""

    async def index_attachment(self, attachment: Attachment, chunks: list[FileChunk]) -> None:
        return None

    async def delete_attachment(self, attachment_id: str) -> None:
        return None

    async def index(self, attachment: Attachment, chunks: list[FileChunk]) -> None:
        return await self.index_attachment(attachment, chunks)

    async def delete(self, attachment_id: str) -> None:
        return await self.delete_attachment(attachment_id)


__all__ = ["DisabledGraphIndexer", "DisabledVectorIndexer"]
