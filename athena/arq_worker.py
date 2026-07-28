"""ARQ Worker configuration for Athena background tasks.

Replaces Celery with ARQ (async-redis-queue) for Windows compatibility.
ARQ is a lightweight async task queue built on Redis and asyncio.

Usage:
    arq athena.arq_worker.WorkerSettings

To dispatch a task from your FastAPI code:
    from arq import create_pool
    from arq.connections import RedisSettings

    redis = await create_pool(RedisSettings())
    job = await redis.enqueue_job(
        'extract_conversation_insights',
        session_id, user_id,
        _job_timeout=300,
        _max_tries=2,
    )
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from athena.config import get_config
from athena.logging_config import get_logger

logger = get_logger(__name__)


async def extract_conversation_insights(
    ctx: dict[str, Any],
    session_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Extract valuable information from a conversation and store as memories.

    This is the ARQ-compatible async version of the former Celery task.

    Steps:
    1. Read messages from LangGraph checkpointer
    2. Trim to complete turns
    3. Check if enough new messages since last extraction
    4. Read existing memories for dedup context
    5. Call fast LLM for extraction
    6. Deduplicate and write to SQLite + ChromaDB directly
    7. Update session's last_extracted_message_count
    """
    import time

    from athena.tasks.conversation_extract import (
        _dedup_and_write,
        _extract_with_llm,
        _format_existing_memories,
        _format_messages_for_llm,
        _trim_to_complete_turns,
    )

    start = time.time()
    logger.info(
        "conversation_extract_started",
        session_id=session_id,
        user_id=user_id,
        job_try=ctx.get("job_try", 1),
    )

    try:
        from athena.core.graph.agent_graph import create_checkpointer
        from athena.core.llm_provider.manager import get_llm_manager
        from athena.core.rag import get_rag_manager
        from athena.models import get_session_maker
        from athena.models.session import Session

        config = get_config()

        # ── 1. Read messages from checkpointer ──────────────────────
        checkpointer = None
        try:
            checkpointer = await create_checkpointer(config.sqlite_db_path)
            checkpoint_tuple = await checkpointer.aget_tuple(
                {"configurable": {"thread_id": session_id}}
            )
        finally:
            if checkpointer is not None:
                await checkpointer.conn.close()

        if checkpoint_tuple is None:
            logger.info("extraction_no_checkpoint", session_id=session_id)
            return {"status": "skipped", "reason": "no_checkpoint"}

        channel_values = checkpoint_tuple.checkpoint.get("channel_values", {})
        all_messages = channel_values.get("messages", [])

        if not all_messages:
            logger.info("extraction_no_messages", session_id=session_id)
            return {"status": "skipped", "reason": "no_messages"}

        # ── 2. Check threshold ──────────────────────────────────────
        session_maker = get_session_maker(config.sqlite_db_path)
        async with session_maker() as db:
            from sqlalchemy import select

            result = await db.execute(
                select(Session).where(Session.session_id == session_id)
            )
            session_row = result.scalar_one_or_none()

        if session_row is None:
            logger.warning("extraction_session_not_found", session_id=session_id)
            return {"status": "skipped", "reason": "session_not_found"}

        last_extracted = session_row.last_extracted_message_count or 0
        new_message_count = len(all_messages) - last_extracted

        if new_message_count < config.system.extraction_min_messages:
            logger.info(
                "extraction_below_threshold",
                session_id=session_id,
                new_messages=new_message_count,
                threshold=config.system.extraction_min_messages,
            )
            return {
                "status": "skipped",
                "reason": "below_threshold",
                "new_messages": new_message_count,
            }

        # ── 3. Trim to complete turns ──────────────────────────────
        recent_messages = all_messages[last_extracted:]
        if len(recent_messages) > config.system.extraction_max_messages:
            recent_messages = recent_messages[: config.system.extraction_max_messages]

        trimmed, is_incomplete = _trim_to_complete_turns(recent_messages)

        if not trimmed:
            return {"status": "skipped", "reason": "no_messages_after_trim"}

        # ── 4. Read existing memories for dedup context ─────────────
        rag_manager = get_rag_manager()
        existing_memories = await rag_manager.list_vectors(user_id, limit=50)
        existing_memories_text = _format_existing_memories(existing_memories)

        # ── 5. LLM extraction ──────────────────────────────────────
        llm_manager = get_llm_manager()
        fast_model = llm_manager.fast_model
        if fast_model is None:
            logger.error("extraction_no_fast_model")
            return {"status": "skipped", "reason": "no_fast_model"}

        conversation_text = _format_messages_for_llm(trimmed)
        if not conversation_text.strip():
            return {"status": "skipped", "reason": "empty_conversation"}

        logger.info(
            "extraction_calling_llm",
            session_id=session_id,
            message_count=len(trimmed),
            is_incomplete=is_incomplete,
        )

        extracted = await _extract_with_llm(
            conversation_text,
            existing_memories_text,
            is_incomplete,
            fast_model,
        )

        facts = extracted.atomic_facts
        summaries = extracted.summaries

        if not facts and not summaries:
            logger.info("extraction_nothing_extracted", session_id=session_id)
            async with session_maker() as db:
                from sqlalchemy import update

                await db.execute(
                    update(Session)
                    .where(Session.session_id == session_id)
                    .values(
                        last_extracted_message_count=last_extracted
                        + len(trimmed)
                    )
                )
                await db.commit()
            return {"status": "completed", "facts": 0, "summaries": 0}

        # ── 6. Dedup & write ───────────────────────────────────────
        written = await _dedup_and_write(
            user_id=user_id,
            facts=facts,
            summaries=summaries,
            session_id=session_id,
            rag_manager=rag_manager,
        )

        # ── 7. Update session ──────────────────────────────────────
        async with session_maker() as db:
            from sqlalchemy import update

            await db.execute(
                update(Session)
                .where(Session.session_id == session_id)
                .values(
                    last_extracted_message_count=last_extracted + len(trimmed)
                )
            )
            await db.commit()

        elapsed = time.time() - start
        logger.info(
            "extraction_completed",
            session_id=session_id,
            user_id=user_id,
            facts=len(facts),
            summaries=len(summaries),
            written=written,
            elapsed_s=elapsed,
        )

        return {
            "status": "completed",
            "facts": len(facts),
            "summaries": len(summaries),
            "written": written,
            "elapsed_s": elapsed,
        }

    except Exception:
        elapsed = time.time() - start
        logger.exception(
            "extraction_task_failed",
            session_id=session_id,
            elapsed_s=elapsed,
        )
        raise


async def startup(ctx: dict[str, Any]) -> None:
    """Initialize resources when the ARQ worker starts up."""
    cfg = get_config()
    # Ensure config is loaded
    logger.info(
        "arq_worker_started",
        redis_url=cfg.redis_url,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    """Cleanup resources when the ARQ worker shuts down."""
    logger.info("arq_worker_shutdown")


def _get_redis_settings() -> RedisSettings:
    """Build RedisSettings from environment/config.

    Uses the CELERY_BROKER_URL environment variable for backward
    compatibility, falling back to REDIS_URL database 1.
    """
    config = get_config()
    broker_url = config.arq_broker_url

    # Parse redis://host:port/dbnum
    # Default: redis://localhost:6379/1
    url = os.environ.get("ARQ_BROKER_URL") or broker_url

    # Parse the URL
    if url.startswith("redis://"):
        parts = url[len("redis://"):].split("/")
        host_port = parts[0].split(":")
        host = host_port[0] or "localhost"
        port = int(host_port[1]) if len(host_port) > 1 else 6379
        database = int(parts[1]) if len(parts) > 1 else 1
    else:
        host, port, database = "localhost", 6379, 1

    return RedisSettings(
        host=host,
        port=port,
        database=database,
    )


class WorkerSettings:
    """ARQ Worker configuration for Athena.

    This class is the entry point for the ARQ worker.

    Run with:
        arq athena.arq_worker.WorkerSettings

    Or programmatically:
        from arq import run_worker
        run_worker(WorkerSettings)
    """

    functions = [extract_conversation_insights]

    on_startup = startup
    on_shutdown = shutdown

    redis_settings = _get_redis_settings()

    # Job execution timeout in seconds
    job_timeout = 600  # 10 minutes

    # Maximum number of concurrent jobs
    max_tries = 2
