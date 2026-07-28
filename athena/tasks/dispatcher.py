"""ARQ task dispatch helper.

Provides a fire-and-forget way to enqueue ARQ jobs from anywhere
in the application, without needing to manage a Redis pool manually.

Usage:
    from athena.tasks.dispatcher import dispatch_extract_conversation

    await dispatch_extract_conversation(session_id, user_id)
"""

from __future__ import annotations

from typing import Any

from athena.config import get_config
from athena.logging_config import get_logger

logger = get_logger(__name__)

# Module-level pool cache to avoid creating new connections for each dispatch
_pool: Any = None


async def _get_pool() -> Any:
    """Get or create an ARQ Redis connection pool."""
    global _pool
    if _pool is None:
        from arq import create_pool
        from arq.connections import RedisSettings

        config = get_config()
        broker_url = config.arq_broker_url

        # Parse redis://host:port/dbnum
        host = "localhost"
        port = 6379
        database = 1

        if broker_url.startswith("redis://"):
            parts = broker_url[len("redis://") :].split("/")
            host_port = parts[0].split(":")
            host = host_port[0] or "localhost"
            if len(host_port) > 1:
                port = int(host_port[1])
            if len(parts) > 1:
                database = int(parts[1])

        _pool = await create_pool(
            RedisSettings(host=host, port=port, database=database)
        )
    return _pool


async def dispatch_extract_conversation(
    session_id: str,
    user_id: str,
) -> str | None:
    """Enqueue a conversation insight extraction job via ARQ.

    Fire-and-forget: failures are logged but not raised, so the
    calling code (SSE stream handler) is never blocked by queue issues.

    Returns the ARQ job ID on success, or None on failure.
    """
    try:
        pool = await _get_pool()
        job = await pool.enqueue_job(
            "extract_conversation_insights",
            session_id,
            user_id,
            _job_timeout=600,  # 10 minutes
            _max_tries=2,
        )
        logger.info(
            "arq_job_enqueued",
            job_id=job.job_id,
            session_id=session_id,
            user_id=user_id,
        )
        return job.job_id
    except Exception:
        logger.exception(
            "arq_dispatch_failed",
            session_id=session_id,
            user_id=user_id,
        )
        return None
