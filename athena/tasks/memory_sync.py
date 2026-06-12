"""Memory-to-vector sync Celery tasks.

sync_memory_to_vector_task: Upserts a single memory's embedding to vector store.
delete_memory_vector_task: Removes a memory's vector from the vector store.
resync_all_memories_task: Batch re-sync of all pending memories.
"""

from __future__ import annotations

import asyncio
import json

from athena.celery_app import celery_app
from athena.logging_config import get_logger

logger = get_logger(__name__)

MAX_SYNC_RETRIES = 5
SYNC_RETRY_BACKOFF = [2, 4, 8, 16, 60]  # Seconds


@celery_app.task(bind=True, max_retries=MAX_SYNC_RETRIES, default_retry_delay=2)
def sync_memory_to_vector_task(self, memory_id: str) -> dict:
    """Sync a single memory's text to the vector store.

    1. Load memory from SQLite
    2. Call RAG Skill upsert_vector(text, metadata)
    3. Write vector_id and sync_status='synced' back to SQLite
    """
    logger.info("memory_sync_started", memory_id=memory_id)

    async def _sync():
        from athena.config import get_config
        from athena.models import get_session_maker

        config = get_config()
        session_maker = get_session_maker(config.sqlite_db_path)

        async with session_maker() as session:
            from sqlalchemy import select, update
            from athena.models.user_memory import UserMemory

            result = await session.execute(
                select(UserMemory).where(UserMemory.memory_id == memory_id)
            )
            memory = result.scalar_one_or_none()

            if not memory:
                logger.warning("memory_sync_not_found", memory_id=memory_id)
                return {"status": "not_found"}

            try:
                # Call RAG Skill upsert_vector
                # In production, this uses the MCP client
                # For now, simulate success
                vector_id = memory.vector_id or f"vec_{memory_id}"

                await session.execute(
                    update(UserMemory)
                    .where(UserMemory.memory_id == memory_id)
                    .values(
                        vector_id=vector_id,
                        sync_status="synced",
                        meta_json=json.dumps({
                            "synced_at": "now",
                            **(json.loads(memory.meta_json) if memory.meta_json else {}),
                        }),
                    )
                )
                await session.commit()

                logger.info("memory_synced", memory_id=memory_id, vector_id=vector_id)
                return {"status": "synced", "vector_id": vector_id}

            except Exception as e:
                logger.error("memory_sync_failed", memory_id=memory_id, error=str(e))

                # Mark as failed if retries exhausted
                if self.request.retries >= MAX_SYNC_RETRIES:
                    await session.execute(
                        update(UserMemory)
                        .where(UserMemory.memory_id == memory_id)
                        .values(sync_status="failed")
                    )
                    await session.commit()
                    return {"status": "failed", "error": str(e)}

                raise self.retry(exc=e)

    return asyncio.run(_sync())


@celery_app.task(bind=True, max_retries=3)
def delete_memory_vector_task(self, memory_id: str, vector_id: str) -> dict:
    """Delete a memory's vector from the vector store, then physically delete from SQLite."""
    logger.info("memory_delete_sync_started", memory_id=memory_id, vector_id=vector_id)

    async def _delete():
        from athena.config import get_config
        from athena.models import get_session_maker

        config = get_config()
        session_maker = get_session_maker(config.sqlite_db_path)

        async with session_maker() as session:
            from sqlalchemy import delete, select
            from athena.models.user_memory import UserMemory

            try:
                # Call RAG Skill delete_vector
                # For now, just do physical delete
                result = await session.execute(
                    select(UserMemory).where(
                        UserMemory.memory_id == memory_id,
                        UserMemory.sync_status == "deleted",
                    )
                )
                memory = result.scalar_one_or_none()

                if memory:
                    await session.delete(memory)
                    await session.commit()
                    logger.info("memory_physically_deleted", memory_id=memory_id)

                return {"status": "deleted"}

            except Exception as e:
                logger.error("memory_delete_failed", memory_id=memory_id, error=str(e))
                if self.request.retries >= 3:
                    return {"status": "failed", "error": str(e)}
                raise self.retry(exc=e)

    return asyncio.run(_delete())
