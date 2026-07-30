"""Task dispatch helper — fire-and-forget background task scheduling.

Uses asyncio-based TaskScheduler instead of ARQ/Redis for lightweight
background task execution without external broker dependencies.
"""

from __future__ import annotations

from athena.logging_config import get_logger
from athena.tasks.scheduler import get_scheduler

logger = get_logger(__name__)


async def dispatch_extract_conversation(
    session_id: str,
    user_id: str,
) -> str | None:
    """Enqueue a conversation insight extraction job via asyncio scheduler.

    Fire-and-forget: failures are logged but not raised, so the
    calling code (SSE stream handler) is never blocked.

    Returns the task job ID on success, or None on failure.
    """
    try:
        from athena.arq_worker import extract_conversation_insights

        scheduler = get_scheduler()
        job_id = await scheduler.enqueue(
            extract_conversation_insights,
            {"job_try": 1},
            session_id,
            user_id,
        )
        logger.info(
            "task_enqueued",
            job_id=job_id,
            session_id=session_id,
            user_id=user_id,
        )
        return job_id
    except Exception:
        logger.exception(
            "task_dispatch_failed",
            session_id=session_id,
            user_id=user_id,
        )
        return None
