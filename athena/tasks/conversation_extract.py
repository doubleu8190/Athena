"""Conversation insight extraction — helper functions for ARQ background tasks.

Reads the latest conversation from the LangGraph checkpointer, uses the
fast LLM to extract valuable information (atomic facts + paragraph
summaries), and stores them directly in ChromaDB via RAGManager.

Triggered after each complete SSE stream in api/im.py via ARQ.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from athena.core.prompt_loader import load_prompt
from athena.core.rag import RAGManager
from athena.logging_config import get_logger

logger = get_logger(__name__)


# ── Data classes ────────────────────────────────────────────────────────────


@dataclass
class AtomicFact:
    """An atomic fact extracted from conversation."""

    key: str = ""
    value: str = ""
    category: str = "other"
    confidence: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> AtomicFact:
        return cls(
            key=data.get("key", ""),
            value=data.get("value", ""),
            category=data.get("category", "other"),
            confidence=data.get("confidence", 0),
        )

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "category": self.category,
            "confidence": self.confidence,
        }


@dataclass
class SummaryItem:
    """A summary extracted from conversation."""

    topic: str = ""
    content: str = ""
    category: str = "other"
    confidence: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> SummaryItem:
        return cls(
            topic=data.get("topic", ""),
            content=data.get("content", ""),
            category=data.get("category", "other"),
            confidence=data.get("confidence", 0),
        )

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "content": self.content,
            "category": self.category,
            "confidence": self.confidence,
        }


@dataclass
class ExtractionResult:
    """Result of LLM-based conversation extraction."""

    atomic_facts: list[AtomicFact] = field(default_factory=list)
    summaries: list[SummaryItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "atomic_facts": [f.to_dict() for f in self.atomic_facts],
            "summaries": [s.to_dict() for s in self.summaries],
        }


# ── Prompt ──────────────────────────────────────────────────────────────────

_EXTRACTION_PROMPT = load_prompt("extraction.md")

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
    """Format existing memories for the prompt context.

    Each memory is a MemoryVector with 'text' and 'metadata' attributes.
    """
    if not memories:
        return "(none)"

    parts: list[str] = []
    for m in memories:
        key = m.metadata.get("key", "")
        value = m.text
        parts.append(f"- {key}: {value}")
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
) -> ExtractionResult:
    """Call the fast LLM to extract insights from conversation text.

    Returns an ``ExtractionResult`` with typed ``atomic_facts`` and
    ``summaries`` lists.  Returns an empty result on parse failure.
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
        raw_facts = parsed.get("atomic_facts", [])
        raw_summaries = parsed.get("summaries", [])

        # Filter by confidence and convert to typed dataclasses
        facts = [
            AtomicFact.from_dict(f)
            for f in raw_facts
            if f.get("confidence", 0) >= _MIN_CONFIDENCE
        ]
        summaries = [
            SummaryItem.from_dict(s)
            for s in raw_summaries
            if s.get("confidence", 0) >= _MIN_CONFIDENCE
        ]

        return ExtractionResult(atomic_facts=facts, summaries=summaries)

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("extraction_parse_failed", error=str(e))
        return ExtractionResult()
    except Exception as e:
        logger.error("extraction_llm_error", error=str(e))
        raise


# ── Dedup & write ───────────────────────────────────────────────────────────


async def _dedup_and_write(
    user_id: str,
    facts: list[AtomicFact],
    summaries: list[SummaryItem],
    session_id: str,
    rag_manager : RAGManager,
) -> int:
    """Write extracted insights to ChromaDB with deduplication.

    For each item, performs a semantic search first:
    - If similarity > threshold → update existing memory (same key)
    - Otherwise → create new memory

    Returns the number of memories created or updated.
    """
    written = 0
    now = datetime.now(UTC).isoformat()

    for fact in facts:
        if not fact.key or not fact.value:
            continue

        # Deterministic key for atomic facts
        memory_key = f"auto_{fact.category}_{_short_hash(fact.key + fact.value)}"

        # Dedup via semantic search (only against other atomic_facts)
        similar = await rag_manager.semantic_search(
            user_id, fact.value, top_k=1, where={"type": "atomic_fact"},
        )
        if similar and similar[0].score >= _DEDUP_SIMILARITY_THRESHOLD:
            memory_key = similar[0].metadata.get("key", memory_key)
            logger.info("extraction_dedup_update", key=memory_key, score=similar[0].score)

        meta = {
            "key": memory_key,
            "type": "atomic_fact",
            "category": fact.category,
            "source": "conversation_extract",
            "session_id": session_id,
            "extracted_at": now,
            "confidence": fact.confidence,
        }
        await rag_manager.upsert_vector(
            memory_id=memory_key, user_id=user_id, text=fact.value, metadata=meta,
        )
        written += 1

    for summary in summaries:
        if not summary.topic or not summary.content:
            continue

        memory_key = f"summary_{summary.category}_{_short_hash(summary.topic)}"

        # Dedup via semantic search (only against other paragraph_summaries)
        similar = await rag_manager.semantic_search(
            user_id, summary.content, top_k=1, where={"type": "paragraph_summary"},
        )
        if similar and similar[0].score >= _DEDUP_SIMILARITY_THRESHOLD:
            memory_key = similar[0].metadata.get("key", memory_key)
            logger.info("extraction_dedup_update_summary", key=memory_key, score=similar[0].score)

        meta = {
            "key": memory_key,
            "type": "paragraph_summary",
            "category": summary.category,
            "source": "conversation_extract",
            "session_id": session_id,
            "extracted_at": now,
        }
        await rag_manager.upsert_vector(
            memory_id=memory_key, user_id=user_id, text=summary.content, metadata=meta,
        )
        written += 1

    return written
