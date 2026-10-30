"""PostgreSQL vector indexers for files and memories."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select, update

from domain.files import Attachment, FileChunk, FileVectorSearchPort, VectorIndexerPort
from domain.memory import MemoryVectorSearchPort
from domain.leases import LeaseGrant, LeaseKey
from infrastructure.persistence.postgres.models import AttachmentModel, FileChunkModel, MemoryModel
from infrastructure.persistence.postgres.repositories.lease_center import PostgresLeaseCenter


class PostgresFileVectorIndex(VectorIndexerPort, FileVectorSearchPort):
    def __init__(self, session_factory, embedder, *, lease_center: PostgresLeaseCenter | None = None) -> None:
        self._session_factory = session_factory
        self._embedder = embedder
        self._leases = lease_center or PostgresLeaseCenter(session_factory)

    async def index_attachment(self, attachment: Attachment, chunks: list[FileChunk], *, lease: LeaseGrant) -> None:
        if lease.key != LeaseKey("document_ingestion", attachment.id):
            raise ValueError("document_ingestion_lease_mismatch")
        if any(chunk.attachment_id != attachment.id for chunk in chunks):
            raise ValueError("document_chunk_attachment_mismatch")
        vectors = await self._embedder.embed_documents([item.content for item in chunks])
        async with self._session_factory() as session:
            async with session.begin():
                attachment_row = await session.scalar(
                    select(AttachmentModel).where(AttachmentModel.id == attachment.id).with_for_update()
                )
                if attachment_row is None or attachment_row.deleted_time is not None:
                    raise LookupError("attachment_deleted")
                chunk_ids = [chunk.id for chunk in chunks]
                if chunk_ids:
                    await session.scalars(
                        select(FileChunkModel)
                        .where(FileChunkModel.id.in_(chunk_ids))
                        .order_by(FileChunkModel.id)
                        .with_for_update()
                    )
                await self._leases.assert_current_in_transaction(session, lease)
                for chunk, vector in zip(chunks, vectors):
                    await session.execute(
                        update(FileChunkModel)
                        .where(
                            FileChunkModel.id == chunk.id,
                            FileChunkModel.attachment_id == attachment.id,
                            FileChunkModel.index_revision_id == chunk.index_revision_id,
                        )
                        .values(embedding=vector)
                    )

    async def delete_attachment(self, attachment_id: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(FileChunkModel)
                    .where(FileChunkModel.attachment_id == attachment_id)
                    .values(embedding=None)
                )

    async def query(
        self,
        text: str,
        limit: int = 10,
        *,
        attachment_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        vector = await self._embedder.embed_query(text)
        async with self._session_factory() as session:
            distance = FileChunkModel.embedding.cosine_distance(vector).label("distance")
            statement = select(FileChunkModel, distance).join(
                AttachmentModel, AttachmentModel.id == FileChunkModel.attachment_id
            ).where(
                FileChunkModel.embedding.is_not(None),
                FileChunkModel.deleted_time.is_(None),
                FileChunkModel.index_revision_id == AttachmentModel.active_index_revision_id,
                AttachmentModel.deleted_time.is_(None),
            )
            if attachment_ids:
                statement = statement.where(FileChunkModel.attachment_id.in_(attachment_ids))
            rows = (await session.execute(statement.order_by(distance).limit(max(0, limit)))).all()
        return [
            {"chunk_id": row[0].id, "content": row[0].content, "score": 1.0 - float(row[1])}
            for row in rows
        ]


class PostgresMemoryVectorIndex(MemoryVectorSearchPort):
    def __init__(self, session_factory, embedder) -> None:
        self._session_factory = session_factory
        self._embedder = embedder

    async def query(
        self, text: str, limit: int = 10, *, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        vector = await self._embedder.embed_query(text)
        async with self._session_factory() as session:
            distance = MemoryModel.embedding.cosine_distance(vector).label("distance")
            statement = select(MemoryModel, distance).where(
                MemoryModel.embedding.is_not(None),
                MemoryModel.deleted_time.is_(None),
                MemoryModel.status == "active",
                MemoryModel.validity_status != "invalid",
                *[or_(column.is_(None), column > func.now())
                  for column in (MemoryModel.expires_at, MemoryModel.valid_until)],
            )
            if session_id:
                statement = statement.where(MemoryModel.session_id == session_id)
            rows = (await session.execute(statement.order_by(distance).limit(max(0, limit)))).all()
        return [
            {"memory_id": row[0].id, "content": row[0].content, "score": 1.0 - float(row[1])}
            for row in rows
        ]

__all__ = [
    "PostgresFileVectorIndex",
    "PostgresMemoryVectorIndex",
]
