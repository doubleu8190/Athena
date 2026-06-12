"""Fault recovery Celery tasks.

recover_orphaned_tasks: Scans for abandoned running tasks and recovers them.
Uses optimistic locking: UPDATE tasks SET status='recovering' WHERE status='running'.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from athena.celery_app import celery_app
from athena.logging_config import get_logger

logger = get_logger(__name__)


@celery_app.task
def recover_orphaned_tasks() -> dict:
    """Periodic task: scan for orphaned tasks and attempt recovery.

    Finds tasks WHERE status='running' AND updated_at is stale (>10 min).
    Uses optimistic locking (UPDATE ... WHERE status='running') to ensure
    only one Worker claims each orphaned task.
    """
    logger.info("recovery_scan_started")

    async def _scan_and_recover():
        from athena.config import get_config
        from athena.models import get_session_maker

        config = get_config()
        session_maker = get_session_maker(config.sqlite_db_path)

        async with session_maker() as session:
            from sqlalchemy import select, update
            from athena.models.task import Task

            # Find stale running tasks (not updated in >10 minutes)
            stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)

            result = await session.execute(
                select(Task.task_id).where(
                    Task.status == "running",
                    Task.updated_at < stale_cutoff,
                )
            )
            orphan_ids = [row[0] for row in result.all()]

        recovered = 0
        abandoned = 0

        for task_id in orphan_ids:
            async with session_maker() as session:
                # Optimistic lock
                result = await session.execute(
                    update(Task)
                    .where(
                        Task.task_id == task_id,
                        Task.status == "running",
                    )
                    .values(
                        status="recovering",
                        recovery_attempts=Task.recovery_attempts + 1,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()

                if result.rowcount == 0:
                    continue  # Another Worker claimed it

                # Check recovery limit
                result = await session.execute(
                    select(Task).where(Task.task_id == task_id)
                )
                task = result.scalar_one_or_none()

                if task and task.recovery_attempts > task.max_recovery_attempts:
                    # Mark as abandoned
                    await session.execute(
                        update(Task)
                        .where(Task.task_id == task_id)
                        .values(
                            status="abandoned",
                            completed_at=datetime.now(timezone.utc),
                        )
                    )
                    await session.commit()
                    abandoned += 1
                    logger.warning(
                        "task_abandoned",
                        task_id=task_id,
                        recovery_attempts=task.recovery_attempts,
                    )
                    continue

                recovered += 1

        logger.info(
            "recovery_scan_completed",
            orphans_found=len(orphan_ids),
            recovered=recovered,
            abandoned=abandoned,
        )

        return {
            "orphans_found": len(orphan_ids),
            "recovered": recovered,
            "abandoned": abandoned,
        }

    return asyncio.run(_scan_and_recover())
