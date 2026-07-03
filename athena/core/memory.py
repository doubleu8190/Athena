"""Memory Store — user memory management with async vector sync.

Architecture:
- SQLite stores memory metadata and original text (synchronous).
- Vector embeddings managed by RAGManager (Chroma) via athena.core.rag.
- Async eventually-consistent sync: SQLite write → Celery task → Vector store → SQLite writeback.

Sync status lifecycle:
    pending → (Celery upsert_vector) → synced
    pending → (retry exhausted) → failed
    synced → (update) → pending (new upsert needed)
    any → (delete) → deleted → (Celery delete_vector) → physical delete from SQLite

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
    sync_status: str = "pending"  # 'pending', 'synced', 'failed', 'deleted'
    meta_json: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""


class MemoryStore:
    """Manages user memories with async vector sync.

    Provides:
    - semantic_search: via RAGManager (Chroma vector search)
    - simple_query: key-based SQLite query
    - upsert: write memory + enqueue vector sync
    - delete: mark for deletion + enqueue vector deletion
    """

    def __init__(self, config: Config, rag_manager: RAGManager | None = None) -> None:
        self.config = config
        self._rag_manager = rag_manager

    # ── Search ────────────────────────────────────────────────────────

    async def semantic_search(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
    ) -> list[Memory]:
        """Semantic search via RAG Skill vector store.

        Returns results from the vector store only. Does NOT fall back to
        simple_query — if RAG is unavailable, returns an empty list.
        Keyword-based retrieval and semantic search are fundamentally
        different models; a silent fallback would inject irrelevant
        memories and mislead the caller about result quality.

        Results include a "source" annotation in meta_json.
        """
        try:
            results = await self._vector_search(user_id, query, top_k)
            for r in results:
                r.meta_json["source"] = "vector"
            return results
        except NotImplementedError:
            logger.info(
                "vector_search_unavailable",
                user_id=user_id,
            )
        except Exception as e:
            logger.error(
                "vector_search_error",
                error=str(e),
                user_id=user_id,
            )

        return []

    async def _vector_search(
        self,
        user_id: str,
        query: str,
        top_k: int,
    ) -> list[Memory]:
        """Internal: search Chroma via RAGManager.

        Raises NotImplementedError if RAGManager is not configured.
        """
        if self._rag_manager is None:
            raise NotImplementedError(
                "Vector search requires RAGManager to be configured"
            )

        results = await self._rag_manager.semantic_search(
            user_id=user_id,
            query=query,
            top_k=top_k,
        )

        # Map RAGManager dict results to Memory objects
        memories: list[Memory] = []
        for item in results:
            meta = item.get("metadata", {})
            memories.append(Memory(
                memory_id=item["memory_id"],
                user_id=user_id,
                key=meta.get("key", ""),
                value=item.get("text", ""),
                vector_id=item["memory_id"],  # Chroma uses memory_id as doc ID
                sync_status="synced",
                meta_json={
                    "source": "vector",
                    "score": item.get("score", 0.0),
                    **meta,
                },
            ))
        return memories

    async def simple_query(
        self,
        user_id: str,
        key_prefix: str = "",
        limit: int = 50,
    ) -> list[Memory]:
        """Key-based simple query from SQLite.

        Ordered by updated_at descending. Used as fallback when
        vector search is unavailable.
        """
        db_path = self.config.sqlite_db_path
        session_maker = get_session_maker(db_path)

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
                sync_status=r.sync_status,
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
        """Insert or update a memory.

        Steps:
        1. SQLite write (synchronous) with sync_status='pending'
        2. Enqueue async Celery task for vector upsert
        """
        db_path = self.config.sqlite_db_path
        session_maker = get_session_maker(db_path)

        # Check if memory with same key exists
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

            meta_json = json.dumps(meta or {}, ensure_ascii=False)

            if existing:
                memory_id = existing.memory_id
                await session.execute(
                    update(UserMemory)
                    .where(UserMemory.memory_id == memory_id)
                    .values(
                        value=value,
                        sync_status="pending",
                        meta_json=meta_json,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                logger.info("memory_updated", memory_id=memory_id, key=key)
            else:
                memory_id = f"mem_{uuid.uuid4().hex[:16]}"
                new_memory = UserMemory(
                    memory_id=memory_id,
                    user_id=user_id,
                    key=key,
                    value=value,
                    sync_status="pending",
                    meta_json=meta_json,
                )
                session.add(new_memory)
                logger.info("memory_created", memory_id=memory_id, key=key)

            await session.commit()

        # Enqueue async vector sync (Celery task)
        try:
            from athena.tasks.memory_sync import sync_memory_to_vector_task
            sync_memory_to_vector_task.delay(memory_id)
        except Exception as e:
            logger.warning("memory_sync_enqueue_failed", memory_id=memory_id, error=str(e))

        return Memory(
            memory_id=memory_id,
            user_id=user_id,
            key=key,
            value=value,
            vector_id=existing.vector_id if existing else None,
            sync_status="pending",
            meta_json=meta or {},
        )

    # ── Delete ────────────────────────────────────────────────────────

    async def delete(self, memory_id: str) -> bool:
        """Mark a memory for deletion.

        Steps:
        1. SQLite: set sync_status='deleted' (soft delete)
        2. Enqueue async Celery task to delete vector from vector store
        3. On vector delete success → physical delete from SQLite
        """
        db_path = self.config.sqlite_db_path
        session_maker = get_session_maker(db_path)

        async with session_maker() as session:
            from sqlalchemy import select, update, delete as sqla_delete
            from athena.models.user_memory import UserMemory

            result = await session.execute(
                select(UserMemory).where(UserMemory.memory_id == memory_id)
            )
            memory = result.scalar_one_or_none()

            if not memory:
                return False

            vector_id = memory.vector_id

            if vector_id:
                # Has vector → soft delete, enqueue vector deletion
                await session.execute(
                    update(UserMemory)
                    .where(UserMemory.memory_id == memory_id)
                    .values(sync_status="deleted")
                )
                await session.commit()

                # Enqueue vector deletion
                try:
                    from athena.tasks.memory_sync import delete_memory_vector_task
                    delete_memory_vector_task.delay(memory_id, vector_id)
                except Exception as e:
                    logger.warning(
                        "memory_delete_enqueue_failed",
                        memory_id=memory_id,
                        error=str(e),
                    )
            else:
                # No vector → physical delete immediately
                await session.execute(
                    sqla_delete(UserMemory).where(UserMemory.memory_id == memory_id)
                )
                await session.commit()

            logger.info("memory_deleted", memory_id=memory_id)
            return True

    # ── Status ────────────────────────────────────────────────────────

    async def get_sync_status(self, memory_id: str) -> str:
        """Get the vector sync status for a memory."""
        db_path = self.config.sqlite_db_path
        session_maker = get_session_maker(db_path)

        async with session_maker() as session:
            from sqlalchemy import select
            from athena.models.user_memory import UserMemory
            result = await session.execute(
                select(UserMemory.sync_status).where(
                    UserMemory.memory_id == memory_id
                )
            )
            status = result.scalar_one_or_none()
            return status or "unknown"
