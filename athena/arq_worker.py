"""Background task definitions — conversation extraction.

Formerly ARQ worker tasks; now run via asyncio TaskScheduler.
The ``extract_conversation_insights`` function signature is preserved
for compatibility (ctx dict as first parameter).

Note: the file is named ``arq_worker.py`` for backward compatibility
with existing imports.  It no longer depends on ARQ.
"""

from __future__ import annotations

import time
from typing import Any

from athena.config import get_config
from athena.logging_config import get_logger

logger = get_logger(__name__)


async def extract_conversation_insights(
    ctx: dict[str, Any],
    session_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Extract valuable information from a conversation and store as memories.

    Steps:
    1. Read messages from LangGraph checkpointer
    2. Trim to complete turns
    3. Check if enough new messages since last extraction
    4. Read existing memories for dedup context
    5. Call fast LLM for extraction
    6. Deduplicate and write to SQLite + ChromaDB directly
    7. Update session's last_extracted_message_count
    """
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
