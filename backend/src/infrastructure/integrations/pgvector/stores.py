"""Target-owned pgvector index adapters."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update

from domain.files import Attachment, FileChunk
from infrastructure.persistence.postgres.models import FileChunkModel, MemoryModel


class PostgresFileVectorIndexer:
    """Embed and persist file chunks through injected target repositories."""

    def __init__(self, session_factory, embedder) -> None:
        self._session_factory = session_factory
        self._embedder = embedder

    async def index(self, attachment: Attachment, chunks: list[FileChunk]) -> None:
        vectors = await self._embedder.embed_documents([item.content for item in chunks])
        async with self._session_factory() as session:
            async with session.begin():
                for chunk, vector in zip(chunks, vectors):
                    await session.execute(
                        update(FileChunkModel)
                        .where(FileChunkModel.id == chunk.id)
                        .values(embedding=vector)
                    )

    async def delete(self, attachment_id: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(FileChunkModel)
                    .where(FileChunkModel.attachment_id == attachment_id)
                    .values(embedding=None)
                )

    async def query(self, text: str, limit: int = 10) -> list[dict[str, Any]]:
        vector = await self._embedder.embed_query(text)
        async with self._session_factory() as session:
            distance = FileChunkModel.embedding.cosine_distance(vector).label("distance")
            rows = (await session.execute(
                select(FileChunkModel, distance)
                .where(FileChunkModel.embedding.is_not(None), FileChunkModel.deleted_time.is_(None))
                .order_by(distance).limit(max(0, limit))
            )).all()
        return [{"chunk_id": row[0].id, "content": row[0].content, "score": 1.0 - float(row[1])} for row in rows]


class PostgresMemoryVectorIndexer:
    """Embed and persist memory records through the target Memory table."""

    def __init__(self, session_factory, embedder) -> None:
        self._session_factory = session_factory
        self._embedder = embedder

    async def index(self, memory_id: str, content: str) -> None:
        vector = await self._embedder.embed_query(content)
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(update(MemoryModel).where(MemoryModel.id == memory_id).values(embedding=vector))

    async def query(self, text: str, limit: int = 10) -> list[dict[str, Any]]:
        vector = await self._embedder.embed_query(text)
        async with self._session_factory() as session:
            distance = MemoryModel.embedding.cosine_distance(vector).label("distance")
            rows = (await session.execute(
                select(MemoryModel, distance)
                .where(MemoryModel.embedding.is_not(None), MemoryModel.deleted_time.is_(None))
                .order_by(distance).limit(max(0, limit))
            )).all()
        return [{"memory_id": row[0].id, "content": row[0].content, "score": 1.0 - float(row[1])} for row in rows]


__all__ = ["PostgresFileVectorIndexer", "PostgresMemoryVectorIndexer"]
