"""Periodic cleanup tasks.

- expire_idle_sessions: transitions idle sessions to expired
- retry_failed_syncs: retries failed memory syncs
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from athena.celery_app import celery_app
from athena.logging_config import get_logger

logger = get_logger(__name__)


@celery_app.task
def expire_idle_sessions() -> dict:
    """Periodic task: mark idle sessions as expired.

    Sessions that have been idle for > session_expire_hours are expired.
    """
    logger.info("session_expiry_scan_started")

    async def _expire():
        from athena.config import get_config
        from athena.models import get_session_maker

        config = get_config()
        session_maker = get_session_maker(config.sqlite_db_path)
        expire_hours = config.system.session_expire_hours

        cutoff = datetime.now(timezone.utc) - timedelta(hours=expire_hours)

        async with session_maker() as session:
            from sqlalchemy import update
            from athena.models.session import Session

            result = await session.execute(
                update(Session)
                .where(
                    Session.status == "idle",
                    Session.last_active_at < cutoff,
                )
                .values(status="expired")
            )
            await session.commit()

            count = result.rowcount
            logger.info("session_expiry_completed", expired_count=count)
            return {"expired_count": count}

    return asyncio.run(_expire())


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
            from sqlalchemy import select, update
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
