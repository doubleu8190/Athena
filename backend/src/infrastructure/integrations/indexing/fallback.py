"""Explicit no-op indexers for deployments without external indexes."""

from domain.files import Attachment, FileChunk


class DisabledVectorIndexer:
    """Ignore vector indexing when embeddings are disabled."""

    async def index(self, attachment: Attachment, chunks: list[FileChunk]) -> None:
        return None

    async def delete(self, attachment_id: str) -> None:
        return None


class DisabledGraphIndexer:
    """Ignore graph indexing when Neo4j is disabled."""

    async def index(self, attachment: Attachment, chunks: list[FileChunk]) -> None:
        return None

    async def delete(self, attachment_id: str) -> None:
        return None


__all__ = ["DisabledGraphIndexer", "DisabledVectorIndexer"]
