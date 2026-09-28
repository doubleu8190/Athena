"""File chunk vector storage backed by PostgreSQL pgvector."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select, update

from athena.core.files.contracts import FileRetrievalCandidate
from athena.core.files.ports import FileVectorStore
from athena.core.llm.tokens import EmbeddingTokenCounter, conservative_text_token_count
from athena.infrastructure.embedding import EmbeddingConfig, EmbeddingProvider
from athena.infrastructure.postgre.engine import get_session
from athena.infrastructure.postgre.models import AttachmentModel, FileChunkModel
from athena.models.json_models import FileLocator


class PgVectorFileVectorStore(FileVectorStore):
    def __init__(self, embedding_config: EmbeddingConfig) -> None:
        if embedding_config.dimension != 1024:
            raise ValueError(
                "The current PostgreSQL schema uses Vector(1024); "
                "set EMBEDDING_DIMENSION=1024 before starting Athena"
            )
        self._embedding = EmbeddingProvider(embedding_config)
        self.token_counter = EmbeddingTokenCounter(
            self._embedding.tokenizer or conservative_text_token_count
        )

    async def replace_attachment(
        self, attachment_id: str, chunks: Sequence[Mapping[str, Any]]
    ) -> None:
        ids = [str(item["id"]) for item in chunks]
        texts = [str(item["content"]) for item in chunks]
        vectors = await asyncio.to_thread(self._embedding.embed_documents, texts)
        for vector in vectors:
            self._check_dimension(vector)
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(FileChunkModel)
                    .where(FileChunkModel.attachment_id == attachment_id)
                    .values(embedding=None)
                )
                for chunk_id, vector in zip(ids, vectors):
                    await session.execute(
                        update(FileChunkModel)
                        .where(FileChunkModel.id == chunk_id)
                        .values(embedding=vector)
                    )

    async def delete_attachment(self, attachment_id: str) -> None:
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(FileChunkModel)
                    .where(FileChunkModel.attachment_id == attachment_id)
                    .values(embedding=None)
                )

    async def query(
        self,
        query: str,
        limit: int,
        where: Mapping[str, Any] | None = None,
    ) -> list[FileRetrievalCandidate]:
        vector = await asyncio.to_thread(self._embedding.embed_query, query)
        self._check_dimension(vector)
        async with get_session() as session:
            distance = FileChunkModel.embedding.cosine_distance(vector).label(
                "distance"
            )
            statement = (
                select(FileChunkModel, AttachmentModel.document_version, distance)
                .join(
                    AttachmentModel, AttachmentModel.id == FileChunkModel.attachment_id
                )
                .where(
                    FileChunkModel.embedding.is_not(None),
                    FileChunkModel.deleted_time.is_(None),
                    AttachmentModel.deleted_time.is_(None),
                )
            )
            for key, value in (where or {}).items():
                if key == "document_version":
                    column = AttachmentModel.document_version
                else:
                    column = getattr(FileChunkModel, key, None)
                if column is not None:
                    statement = statement.where(column == value)
            rows = (
                await session.execute(statement.order_by(distance).limit(max(0, limit)))
            ).all()
        items: list[FileRetrievalCandidate] = []
        for row in rows:
            chunk, document_version, raw_distance = row
            try:
                locator = FileLocator.model_validate_json(
                    chunk.locator_json
                ).model_dump(mode="json", exclude_none=True)
            except Exception:
                locator = {}
            raw_metadata = json.loads(chunk.metadata_json or "{}")
            metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
            items.append(
                FileRetrievalCandidate(
                    source_id=chunk.id,
                    content=chunk.content,
                    locator=locator,
                    metadata=metadata,
                    attachment_id=chunk.attachment_id,
                    document_version=document_version,
                    vector_score=1.0 - float(raw_distance),
                )
            )
        return items

    def _check_dimension(self, vector: list[float]) -> None:
        if len(vector) != self._embedding.dimension:
            raise ValueError(
                f"Embedding dimension {len(vector)} does not match configured "
                f"dimension {self._embedding.dimension}"
            )
