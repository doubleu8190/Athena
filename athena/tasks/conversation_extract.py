"""Conversation insight extraction Celery task.

Reads the latest conversation from the LangGraph checkpointer, uses the
fast LLM to extract valuable information (atomic facts + paragraph
summaries), and stores them via MemoryStore (synchronous dual-write to
SQLite + ChromaDB).

Triggered after each complete SSE stream in api/im.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from athena.celery_app import celery_app
from athena.logging_config import get_logger

logger = get_logger(__name__)

# ── Prompt ──────────────────────────────────────────────────────────────────

_EXTRACTION_PROMPT = """You are an information extraction engine. Analyze the following conversation and extract valuable information.

{incomplete_notice}
Existing memories (avoid duplicates):
{existing_memories}

Conversation:
{conversation_text}

Extraction rules:
1. Only extract information with long-term value; ignore one-off Q&A and small talk.
2. Atomic facts: user preferences, habits, personal information, technical choices, etc. (each should be standalone and concise).
3. Paragraph summaries: important discussions, technical decisions, problem solutions (retain key context).
4. Do NOT extract information already present in "Existing memories".
5. Attach a confidence score (0-1) to each item; discard anything below 0.6.
6. If the conversation may be unfinished, only extract confirmed information and skip incomplete discussions.

Output JSON (do NOT wrap in markdown code fences):
{{
  "atomic_facts": [
    {{"key": "short identifier", "value": "fact content", "category": "preference|profile|project|technical_decision|fact|other", "confidence": 0.9}}
  ],
  "summaries": [
    {{"topic": "discussion topic", "content": "summary content", "category": "technical_discussion|problem_solving|planning|other", "confidence": 0.85}}
  ]
}}"""

_INCOMPLETE_NOTICE = (
    "\nNote: The conversation may be unfinished — the last message is an AI reply/question "
    "and the user has not responded yet. Only extract confirmed information and skip "
    "incomplete discussions.\n"
)

# ── Confidence threshold ────────────────────────────────────────────────────

_MIN_CONFIDENCE = 0.6
_DEDUP_SIMILARITY_THRESHOLD = 0.85


# ── Helpers ─────────────────────────────────────────────────────────────────


def _trim_to_complete_turns(
    messages: list,
) -> tuple[list, bool]:
    """Trim messages to the last complete conversation turn.

    Returns ``(trimmed_messages, is_incomplete)``.

    * If the last message is an AIMessage with tool_calls (tool execution
      in progress), the entire last AI turn is removed.
    * If the last message is an AIMessage without tool_calls (likely a
      question or chitchat), all messages are kept but ``is_incomplete``
      is ``True`` so the prompt can annotate it.
    * If the last message is a HumanMessage, the conversation is complete.
    """
    if not messages:
        return messages, False

    last = messages[-1]

    # Tool-call in progress — drop the last AI turn
    if isinstance(last, AIMessage) and last.tool_calls:
        return _drop_last_ai_turn(messages), True

    # AI replied (no tool_calls) but user hasn't responded — incomplete
    if isinstance(last, AIMessage):
        return messages, True

    # Last message is from user — complete
    return messages, False


def _drop_last_ai_turn(messages: list) -> list:
    """Remove the last AI turn (AIMessage + any preceding ToolMessages).

    Walks backwards from the end, removing the trailing AIMessage and any
    ToolMessages that belong to it.  If the result is empty, returns at
    least the first message to avoid passing nothing to the LLM.
    """
    result = list(messages)

    # Pop trailing ToolMessages
    while result and isinstance(result[-1], ToolMessage):
        result.pop()

    # Pop the AIMessage that initiated those tool calls
    if result and isinstance(result[-1], AIMessage):
        result.pop()

    return result if result else messages[:1]


def _format_messages_for_llm(messages: list) -> str:
    """Convert LangChain messages to a plain-text conversation string."""
    parts: list[str] = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            role = "User"
        elif isinstance(msg, AIMessage):
            # Skip empty AI messages (tool-calling stubs)
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if not content.strip() and msg.tool_calls:
                continue
            role = "Assistant"
        elif isinstance(msg, ToolMessage):
            role = "Tool"
        else:
            continue

        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if content.strip():
            parts.append(f"[{role}] {content}")

    return "\n".join(parts)


def _format_existing_memories(memories: list) -> str:
    """Format existing memories for the prompt context."""
    if not memories:
        return "(none)"

    parts: list[str] = []
    for m in memories:
        parts.append(f"- {m['key']}: {m['value']}")
    return "\n".join(parts)


def _short_hash(text: str) -> str:
    """Return a short hash for generating deterministic keys."""
    return hashlib.sha256(text.encode()).hexdigest()[:8]


# ── LLM call ────────────────────────────────────────────────────────────────


async def _extract_with_llm(
    conversation_text: str,
    existing_memories_text: str,
    is_incomplete: bool,
    fast_model,
) -> dict:
    """Call the fast LLM to extract insights from conversation text.

    Returns a dict with ``atomic_facts`` and ``summaries`` lists.
    Returns empty lists on parse failure.
    """
    from langchain_core.messages import HumanMessage

    notice = _INCOMPLETE_NOTICE if is_incomplete else ""
    prompt_text = _EXTRACTION_PROMPT.format(
        incomplete_notice=notice,
        existing_memories=existing_memories_text,
        conversation_text=conversation_text,
    )

    try:
        result = await fast_model.ainvoke(
            [HumanMessage(content=prompt_text)]
        )
        raw = result.content if isinstance(result.content, str) else str(result.content)

        # Strip markdown code fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            # Remove first and last lines
            lines = raw.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines)

        parsed = json.loads(raw)

        # Validate structure
        facts = parsed.get("atomic_facts", [])
        summaries = parsed.get("summaries", [])

        # Filter by confidence
        facts = [f for f in facts if f.get("confidence", 0) >= _MIN_CONFIDENCE]
        summaries = [s for s in summaries if s.get("confidence", 0) >= _MIN_CONFIDENCE]

        return {"atomic_facts": facts, "summaries": summaries}

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("extraction_parse_failed", error=str(e))
        return {"atomic_facts": [], "summaries": []}
    except Exception as e:
        logger.error("extraction_llm_error", error=str(e))
        raise


# ── Dedup & write ───────────────────────────────────────────────────────────


async def _dedup_and_write(
    user_id: str,
    facts: list[dict],
    summaries: list[dict],
    session_id: str,
    rag_manager,
    memory_store,
) -> int:
    """Write extracted insights to MemoryStore with deduplication.

    For each item, performs a semantic search first:
    - If similarity > threshold → update existing memory (same key)
    - Otherwise → create new memory

    Returns the number of memories created or updated.
    """
    written = 0
    now = datetime.now(timezone.utc).isoformat()

    for fact in facts:
        key = fact.get("key", "")
        value = fact.get("value", "")
        category = fact.get("category", "other")

        if not key or not value:
            continue

        # Deterministic key for atomic facts
        memory_key = f"auto_{category}_{_short_hash(key + value)}"

        # Dedup via semantic search (only against other atomic_facts)
        similar = await rag_manager.semantic_search(
            user_id, value, top_k=1, where={"type": "atomic_fact"},
        )
        if similar and similar[0].get("score", 0) >= _DEDUP_SIMILARITY_THRESHOLD:
            memory_key = similar[0].get("metadata", {}).get("key", memory_key)
            logger.info("extraction_dedup_update", key=memory_key, score=similar[0].get("score"))

        meta = {
            "type": "atomic_fact",
            "category": category,
            "source": "conversation_extract",
            "session_id": session_id,
            "extracted_at": now,
            "confidence": fact.get("confidence", 0),
        }
        await memory_store.upsert(user_id=user_id, key=memory_key, value=value, meta=meta)
        written += 1

    for summary in summaries:
        topic = summary.get("topic", "")
        content = summary.get("content", "")
        category = summary.get("category", "other")

        if not topic or not content:
            continue

        memory_key = f"summary_{category}_{_short_hash(topic)}"

        # Dedup via semantic search (only against other paragraph_summaries)
        similar = await rag_manager.semantic_search(
            user_id, content, top_k=1, where={"type": "paragraph_summary"},
        )
        if similar and similar[0].get("score", 0) >= _DEDUP_SIMILARITY_THRESHOLD:
            memory_key = similar[0].get("metadata", {}).get("key", memory_key)
            logger.info("extraction_dedup_update_summary", key=memory_key, score=similar[0].get("score"))

        meta = {
            "type": "paragraph_summary",
            "category": category,
            "source": "conversation_extract",
            "session_id": session_id,
            "extracted_at": now,
        }
        await memory_store.upsert(user_id=user_id, key=memory_key, value=content, meta=meta)
        written += 1

    return written


# ── Celery task ─────────────────────────────────────────────────────────────


@celery_app.task(bind=True, max_retries=2, default_retry_delay=10)
def extract_conversation_insights_task(
    self,
    session_id: str,
    user_id: str,
) -> dict:
    """Extract valuable information from a conversation and store as memories.

    Steps:
    1. Read messages from LangGraph checkpointer
    2. Trim to complete turns (Strategy C)
    3. Check if enough new messages since last extraction
    4. Read existing memories for dedup context
    5. Call fast LLM for extraction
    6. Deduplicate and write to SQLite + ChromaDB directly
    7. Update session's last_extracted_message_count
    """
    logger.info("conversation_extract_started", session_id=session_id, user_id=user_id)

    async def _extract():
        from athena.config import get_config
        from athena.core.graph.agent_graph import create_checkpointer
        from athena.core.llm_provider.manager import get_llm_manager
        from athena.core.rag import get_rag_manager
        from athena.models import get_session_maker
        from athena.models.session import Session

        config = get_config()

        # ── 1. Read messages from checkpointer ──────────────────────────
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

        # ── 2. Check threshold ──────────────────────────────────────────
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
            return {"status": "skipped", "reason": "below_threshold", "new_messages": new_message_count}

        # ── 3. Trim to complete turns ──────────────────────────────────
        recent_messages = all_messages[last_extracted:]
        # Cap at max_messages — process oldest unextracted messages first,
        # remaining messages will be picked up in the next extraction run
        if len(recent_messages) > config.system.extraction_max_messages:
            recent_messages = recent_messages[:config.system.extraction_max_messages]

        trimmed, is_incomplete = _trim_to_complete_turns(recent_messages)

        if not trimmed:
            return {"status": "skipped", "reason": "no_messages_after_trim"}

        # ── 4. Read existing memories for dedup context ────────────────
        from athena.core.memory import MemoryStore

        rag_manager = get_rag_manager()
        memory_store = MemoryStore(config, rag_manager)
        existing_memories = await memory_store.simple_query(user_id, limit=50)
        existing_memories_text = _format_existing_memories(existing_memories)

        # ── 5. LLM extraction ─────────────────────────────────────────
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

        facts = extracted.get("atomic_facts", [])
        summaries = extracted.get("summaries", [])

        if not facts and not summaries:
            logger.info("extraction_nothing_extracted", session_id=session_id)
            # Still update the count so we don't re-extract the same messages
            async with session_maker() as db:
                from sqlalchemy import update
                await db.execute(
                    update(Session)
                    .where(Session.session_id == session_id)
                    .values(last_extracted_message_count=last_extracted + len(trimmed))
                )
                await db.commit()
            return {"status": "completed", "facts": 0, "summaries": 0}

        # ── 6. Dedup & write ──────────────────────────────────────────
        written = await _dedup_and_write(
            user_id=user_id,
            facts=facts,
            summaries=summaries,
            session_id=session_id,
            rag_manager=rag_manager,
            memory_store=memory_store,
        )

        # ── 7. Update session ─────────────────────────────────────────
        # Advance pointer past dropped + trimmed messages (not necessarily len(all_messages),
        # since _trim_to_complete_turns may drop an incomplete tail)
        async with session_maker() as db:
            from sqlalchemy import update
            await db.execute(
                update(Session)
                .where(Session.session_id == session_id)
                .values(last_extracted_message_count=last_extracted + len(trimmed))
            )
            await db.commit()

        logger.info(
            "extraction_completed",
            session_id=session_id,
            user_id=user_id,
            facts=len(facts),
            summaries=len(summaries),
            written=written,
        )

        return {
            "status": "completed",
            "facts": len(facts),
            "summaries": len(summaries),
            "written": written,
        }

    try:
        return asyncio.run(_extract())
    except Exception as e:
        logger.error("extraction_task_failed", session_id=session_id, error=str(e))
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        return {"status": "failed", "error": str(e)}
