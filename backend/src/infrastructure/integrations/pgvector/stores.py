"""Compatibility module for the PostgreSQL vector indexers.

The public capability-level module is :mod:`.indexers`; this module remains for
existing imports during the migration.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update

from domain.files import Attachment, FileChunk
from infrastructure.persistence.postgres.models import FileChunkModel, MemoryModel


class PostgresFileVectorIndex:
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

    async def query(self, text: str, limit: int = 10, *, attachment_ids: set[str] | None = None) -> list[dict[str, Any]]:
        vector = await self._embedder.embed_query(text)
        async with self._session_factory() as session:
            distance = FileChunkModel.embedding.cosine_distance(vector).label("distance")
            statement = select(FileChunkModel, distance).where(FileChunkModel.embedding.is_not(None), FileChunkModel.deleted_time.is_(None))
            if attachment_ids:
                statement = statement.where(FileChunkModel.attachment_id.in_(attachment_ids))
            rows = (await session.execute(statement.order_by(distance).limit(max(0, limit)))).all()
        return [{"chunk_id": row[0].id, "content": row[0].content, "score": 1.0 - float(row[1])} for row in rows]


class PostgresMemoryVectorIndex:
    """Embed and persist memory records through the target Memory table."""

    def __init__(self, session_factory, embedder) -> None:
        self._session_factory = session_factory
        self._embedder = embedder

    async def index(self, memory_id: str, content: str) -> None:
        vector = await self._embedder.embed_query(content)
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(update(MemoryModel).where(MemoryModel.id == memory_id).values(embedding=vector))

    async def query(self, text: str, limit: int = 10, *, session_id: str | None = None) -> list[dict[str, Any]]:
        vector = await self._embedder.embed_query(text)
        async with self._session_factory() as session:
            distance = MemoryModel.embedding.cosine_distance(vector).label("distance")
            statement = select(MemoryModel, distance).where(MemoryModel.embedding.is_not(None), MemoryModel.deleted_time.is_(None))
            if session_id:
                statement = statement.where(MemoryModel.session_id == session_id)
            rows = (await session.execute(statement.order_by(distance).limit(max(0, limit)))).all()
        return [{"memory_id": row[0].id, "content": row[0].content, "score": 1.0 - float(row[1])} for row in rows]


PostgresFileVectorIndexer = PostgresFileVectorIndex
PostgresMemoryVectorIndexer = PostgresMemoryVectorIndex

__all__ = [
    "PostgresFileVectorIndex",
    "PostgresMemoryVectorIndex",
    "PostgresFileVectorIndexer",
    "PostgresMemoryVectorIndexer",
]
