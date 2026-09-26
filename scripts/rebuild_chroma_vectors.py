#!/usr/bin/env python3
"""从 PostgreSQL 重建 Athena 的两个 Chroma 向量 collection。"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from athena.config.settings import get_settings
from athena.infrastructure.chroma.embedding import embedding_config_from_settings
from athena.infrastructure.chroma.file_vector_store import ChromaFileVectorStore
from athena.infrastructure.chroma.memory_vector_store import ChromaMemoryVectorStore
from athena.infrastructure.postgre.database import Database
from athena.infrastructure.postgre.models import AttachmentModel
from athena.infrastructure.postgre.repositories.memory_repository import (
    PostgresMemoryRepository,
)
from athena.infrastructure.postgre.engine import get_session


async def rebuild() -> None:
    settings = get_settings()
    database = Database(settings.postgres_url)
    await database.connect(
        pool_size=settings.postgres_pool_size,
        max_overflow=settings.postgres_max_overflow,
    )
    config = embedding_config_from_settings(settings)
    file_vectors = ChromaFileVectorStore(
        path=str(settings.chroma_path), embedding_config=config
    )
    memory_vectors = ChromaMemoryVectorStore(
        path=str(settings.chroma_path), embedding_config=config
    )
    try:
        await memory_vectors.initialize()
        async with get_session() as session:
            attachments = (
                await session.execute(
                    select(AttachmentModel).where(
                        AttachmentModel.deleted_time.is_(None)
                    )
                )
            ).scalars().all()

        file_count = 0
        chunk_count = 0
        for attachment in attachments:
            chunks = await database.files.get_chunks(attachment.id, limit=100_000)
            if not chunks:
                continue
            await file_vectors.replace_attachment(
                attachment.id,
                [
                    {
                        "id": chunk.id,
                        "content": chunk.content,
                        "metadata": {
                            "attachment_id": attachment.id,
                            "document_version": attachment.document_version,
                            "ordinal": chunk.ordinal,
                            "locator_json": json.dumps(
                                chunk.locator.model_dump(
                                    mode="json", exclude_none=True
                                ),
                                ensure_ascii=False,
                            ),
                        },
                    }
                    for chunk in chunks
                ],
            )
            file_count += 1
            chunk_count += len(chunks)

        memory_repository = PostgresMemoryRepository()
        memories = await memory_repository.list(limit=1_000_000)
        for memory in memories:
            await memory_vectors.add(
                memory_id=str(memory["id"]),
                content=str(memory["content"]),
                metadata={
                    key: value
                    for key, value in dict(memory.get("metadata") or {}).items()
                    if value is not None
                },
            )
        print(
            f"rebuilt files={file_count} chunks={chunk_count} memories={len(memories)}"
        )
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(rebuild())
