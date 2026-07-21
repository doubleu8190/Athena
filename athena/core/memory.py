"""Memory Store — user memory management with synchronous dual-write.

Architecture:
- SQLite stores memory metadata and original text.
- Vector embeddings managed by RAGManager (Chroma) via athena.core.rag.
- Both stores are written to synchronously in each operation.

Semantic search requires RAGManager. Without it, semantic_search returns
an empty list — we don't fall back to keyword search because the two
retrieval models are fundamentally incomparable.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from athena.config import Config
from athena.core.rag import RAGManager
from athena.logging_config import get_logger
from athena.models import get_session_maker

logger = get_logger(__name__)


@dataclass
class Memory:
    """User memory record."""
    memory_id: str
    user_id: str
    key: str
    value: str | None = None
    vector_id: str | None = None
    meta_json: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""


class MemoryStore:
    """Manages user memories with synchronous dual-write to SQLite + ChromaDB.

    Every write operation (upsert/delete) hits both stores in one call.
    No async Celery tasks — writes are immediate and consistent.
    """

    def __init__(self, config: Config, rag_manager: RAGManager) -> None:
        self.config = config
        self._rag_manager = rag_manager

    # ── Search ────────────────────────────────────────────────────────

    async def semantic_search(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[Memory]:
        """Semantic search via RAGManager (Chroma vector search).

        Args:
            user_id: Filter results to this user.
            query: Natural language query to embed and search.
            top_k: Maximum number of results to return.
            where: Additional ChromaDB metadata filter (merged with user_id).
                   Example: {"type": "atomic_fact"} to search only facts.

        Returns Memory objects with score in meta_json["source"].
        """
        try:
            results = await self._rag_manager.semantic_search(
                user_id=user_id,
                query=query,
                top_k=top_k,
                where=where,
            )
            memories: list[Memory] = []
            for item in results:
                meta = item.get("metadata", {})
                memories.append(Memory(
                    memory_id=item["memory_id"],
                    user_id=user_id,
                    key=meta.get("key", ""),
                    value=item.get("text", ""),
                    vector_id=item["memory_id"],
                    meta_json={
                        "source": "vector",
                        "score": item.get("score", 0.0),
                        **meta,
                    },
                ))
            return memories
        except Exception as e:
            logger.error("vector_search_error", error=str(e), user_id=user_id)
            return []

    async def simple_query(
        self,
        user_id: str,
        key_prefix: str = "",
        limit: int = 50,
    ) -> list[Memory]:
        """Key-based simple query from SQLite.

        Ordered by updated_at descending.
        """
        session_maker = get_session_maker(self.config.sqlite_db_path)

        async with session_maker() as session:
            from sqlalchemy import select
            from athena.models.user_memory import UserMemory

            stmt = select(UserMemory).where(
                UserMemory.user_id == user_id,
                UserMemory.sync_status != "deleted",
            )

            if key_prefix:
                stmt = stmt.where(UserMemory.key.startswith(key_prefix))

            stmt = stmt.order_by(UserMemory.updated_at.desc()).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()

        return [
            Memory(
                memory_id=r.memory_id,
                user_id=r.user_id,
                key=r.key,
                value=r.value,
                vector_id=r.vector_id,
                meta_json=json.loads(r.meta_json) if r.meta_json else {},
                updated_at=r.updated_at.isoformat() if r.updated_at else "",
            )
            for r in rows
        ]

    # ── Write ─────────────────────────────────────────────────────────

    async def upsert(
        self,
        user_id: str,
        key: str,
        value: str,
        meta: dict[str, Any] | None = None,
    ) -> Memory:
        """Insert or update a memory — synchronous dual-write to SQLite + ChromaDB."""
        session_maker = get_session_maker(self.config.sqlite_db_path)
        meta_json = json.dumps(meta or {}, ensure_ascii=False)

        # 1. Write to SQLite (upsert by user_id + key)
        async with session_maker() as session:
            from sqlalchemy import select, update
            from athena.models.user_memory import UserMemory

            result = await session.execute(
                select(UserMemory).where(
                    UserMemory.user_id == user_id,
                    UserMemory.key == key,
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                memory_id = existing.memory_id
                await session.execute(
                    update(UserMemory)
                    .where(UserMemory.memory_id == memory_id)
                    .values(
                        value=value,
                        vector_id=memory_id,
                        sync_status="synced",
                        meta_json=meta_json,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                logger.info("memory_updated", memory_id=memory_id, key=key)
            else:
                memory_id = f"mem_{uuid.uuid4().hex[:16]}"
                session.add(UserMemory(
                    memory_id=memory_id,
                    user_id=user_id,
                    key=key,
                    value=value,
                    vector_id=memory_id,
                    sync_status="synced",
                    meta_json=meta_json,
                ))
                logger.info("memory_created", memory_id=memory_id, key=key)

            await session.commit()

        # 2. Write to ChromaDB directly
        await self._rag_manager.upsert_vector(
            memory_id=memory_id,
            text=value,
            user_id=user_id,
            metadata={"key": key, **(meta or {})},
        )

        return Memory(
            memory_id=memory_id,
            user_id=user_id,
            key=key,
            value=value,
            vector_id=memory_id,
            sync_status="synced",
            meta_json=meta or {},
        )

    # ── Delete ────────────────────────────────────────────────────────

    async def delete(self, memory_id: str) -> bool:
        """Delete a memory from both SQLite and ChromaDB synchronously."""
        session_maker = get_session_maker(self.config.sqlite_db_path)

        async with session_maker() as session:
            from sqlalchemy import select, delete as sqla_delete
            from athena.models.user_memory import UserMemory

            result = await session.execute(
                select(UserMemory).where(UserMemory.memory_id == memory_id)
            )
            memory = result.scalar_one_or_none()

            if not memory:
                return False

            # Delete from ChromaDB
            if memory.vector_id:
                try:
                    self._rag_manager.delete_vector(memory.vector_id)
                except Exception as e:
                    logger.warning("vector_delete_failed", memory_id=memory_id, error=str(e))

            # Delete from SQLite
            await session.execute(
                sqla_delete(UserMemory).where(UserMemory.memory_id == memory_id)
            )
            await session.commit()

        logger.info("memory_deleted", memory_id=memory_id)
        return True
