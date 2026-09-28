"""Long-term memory vector storage backed by PostgreSQL pgvector."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select, update

from athena.core.memory.ports import MemoryVectorStore
from athena.infrastructure.embedding import EmbeddingConfig, EmbeddingProvider
from athena.infrastructure.postgre.engine import get_session
from athena.infrastructure.postgre.models import MemoryModel
from athena.infrastructure.postgre.repositories.memory_repository import _metadata


class PgVectorMemoryVectorStore(MemoryVectorStore):
    def __init__(self, embedding_config: EmbeddingConfig) -> None:
        if embedding_config.dimension != 1024:
            raise ValueError(
                "The current PostgreSQL schema uses Vector(1024); "
                "set EMBEDDING_DIMENSION=1024 before starting Athena"
            )
        self._embedding = EmbeddingProvider(embedding_config)
        self._initialized = False

    async def initialize(self) -> None:
        self._initialized = True

    async def add(self, memory_id: str, content: str, metadata: dict[str, Any]) -> None:
        vector = await asyncio.to_thread(self._embedding.embed_query, content)
        self._check_dimension(vector)
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(MemoryModel)
                    .where(MemoryModel.id == memory_id)
                    .values(embedding=vector)
                )

    async def query(
        self, query: str, limit: int, where: dict[str, Any] | None
    ) -> dict[str, Any]:
        vector = await asyncio.to_thread(self._embedding.embed_query, query)
        self._check_dimension(vector)
        async with get_session() as session:
            distance = MemoryModel.embedding.cosine_distance(vector).label("distance")
            statement = select(MemoryModel, distance).where(
                MemoryModel.embedding.is_not(None),
                MemoryModel.deleted_time.is_(None),
                MemoryModel.status == "active",
                MemoryModel.validity_status != "invalid",
                or_(
                    MemoryModel.valid_until.is_(None),
                    MemoryModel.valid_until > datetime.now().isoformat(),
                ),
            )
            for key, value in (where or {}).items():
                column_name = {"type": "memory_type", "source": "source_kind"}.get(key, key)
                column = getattr(MemoryModel, column_name, None)
                if column is not None:
                    statement = statement.where(column == value)
            statement = statement.order_by(distance).limit(max(0, limit))
            rows = (await session.execute(statement)).all()
        return {
            "ids": [[row[0].id for row in rows]],
            "documents": [[row[0].content for row in rows]],
            "metadatas": [[_metadata(row[0]) for row in rows]],
            "distances": [[float(row[1]) for row in rows]],
        }

    async def get(self, memory_id: str) -> dict[str, Any]:
        async with get_session() as session:
            row = (
                await session.execute(
                    select(MemoryModel).where(MemoryModel.id == memory_id)
                )
            ).scalar_one_or_none()
        if row is None or row.embedding is None:
            return {"ids": [], "documents": [], "metadatas": []}
        return {"ids": [row.id], "documents": [row.content], "metadatas": [_metadata(row)]}

    async def update(
        self,
        memory_id: str,
        content: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        # Metadata is already authoritative in the memories row; lifecycle
        # operations update those columns through PostgresMemoryRepository.
        values: dict[str, Any] = {}
        if content is not None:
            vector = await asyncio.to_thread(self._embedding.embed_query, content)
            self._check_dimension(vector)
            values.update(content=content, embedding=vector)
        if values:
            async with get_session() as session:
                async with session.begin():
                    await session.execute(
                        update(MemoryModel).where(MemoryModel.id == memory_id).values(**values)
                    )

    def _check_dimension(self, vector: list[float]) -> None:
        if len(vector) != self._embedding.dimension:
            raise ValueError(
                f"Embedding dimension {len(vector)} does not match configured "
                f"dimension {self._embedding.dimension}"
            )

    async def delete(self, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(MemoryModel)
                    .where(MemoryModel.id.in_(memory_ids))
                    .values(embedding=None)
                )
