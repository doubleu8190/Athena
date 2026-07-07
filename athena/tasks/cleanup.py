"""Periodic cleanup tasks.

- retry_failed_syncs: retries failed memory syncs
"""

from __future__ import annotations

import asyncio

from athena.celery_app import celery_app
from athena.logging_config import get_logger

logger = get_logger(__name__)


@celery_app.task
def retry_failed_syncs() -> dict:
    """Periodic task: retry failed memory syncs."""
    logger.info("retry_failed_syncs_started")

    async def _retry():
        from athena.config import get_config
        from athena.models import get_session_maker

        config = get_config()
        session_maker = get_session_maker(config.sqlite_db_path)

        async with session_maker() as session:
            from sqlalchemy import select
            from athena.models.user_memory import UserMemory

            result = await session.execute(
                select(UserMemory).where(UserMemory.sync_status == "pending")
            )
            pending = result.scalars().all()

            if pending:
                from athena.tasks.memory_sync import sync_memory_to_vector_task
                for memory in pending:
                    sync_memory_to_vector_task.delay(memory.memory_id)

            logger.info("retry_failed_syncs_completed", pending_count=len(pending))
            return {"re_enqueued": len(pending)}

    return asyncio.run(_retry())
