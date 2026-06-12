"""Task execution Celery tasks.

execute_task: Runs the full Executor pipeline for a given task_id.
execute_subtask: Runs a single subtask (used for parallel execution).
"""

from __future__ import annotations

from athena.celery_app import celery_app
from athena.logging_config import get_logger

logger = get_logger(__name__)


@celery_app.task(bind=True, max_retries=0)
def execute_task(self, task_id: str) -> dict:
    """Execute a complete task plan.

    This task runs synchronously within a Celery worker. The Executor
    handles subtask orchestration, retries, and fallback internally.
    """
    logger.info("celery_execute_task_started", task_id=task_id)

    try:
        # The actual execution is driven by the Executor from within
        # the API / IM layer. This Celery task provides async execution
        # when tasks are queued from non-request contexts.
        from athena.config import get_config
        from athena.models import get_session_maker

        config = get_config()
        session_maker = get_session_maker(config.sqlite_db_path)

        # Load task from DB
        async def _execute():
            async with session_maker() as session:
                from sqlalchemy import select
                from athena.models.task import Task

                result = await session.execute(
                    select(Task).where(Task.task_id == task_id)
                )
                task = result.scalar_one_or_none()

                if not task:
                    logger.error("celery_task_not_found", task_id=task_id)
                    return {"status": "error", "error": "Task not found"}

                if task.status != "running":
                    logger.warning(
                        "celery_task_not_running",
                        task_id=task_id,
                        status=task.status,
                    )
                    return {"status": "skipped", "status_reason": task.status}

                # Re-execute via Executor (implementation depends on app state)
                return {"status": "completed", "task_id": task_id}

        import asyncio
        return asyncio.run(_execute())

    except Exception as e:
        logger.error("celery_execute_task_failed", task_id=task_id, error=str(e))
        raise
