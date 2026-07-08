"""Context-aware summarisation node.

Runs **before** every ``agent_node`` call.  Estimates token usage of the
current effective message window and, when it exceeds 80 % of the model's
context window, uses the fast model to produce a cumulative summary.

The summary and a message-count offset are persisted to the session table
so they survive graph restarts and checkpoint boundaries.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage, filter_messages
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables import RunnableConfig
from sqlalchemy import select, update

from athena.core.graph.agent_state import AgentState
from athena.core.graph.nodes.agent import AGENT_SYSTEM_PROMPT
from athena.core.llm_provider.manager import LLMProviderManager
from athena.models.base import get_session
from athena.models.session import Session
from athena.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUMMARIZE_THRESHOLD = 0.8
"""Proportion of context window that triggers summarisation."""

_SUMMARIZE_PROMPT = (
    "You are a concise summarisation engine.  Produce a single cumulative "
    "summary that covers BOTH the previous summary (if any) and the new "
    "messages below.  Preserve all factual claims, decisions, user "
    "preferences, tool outputs, and unresolved questions.  Write in prose, "
    "not bullet points.  Aim for roughly 300-500 words."
)

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _load_session_summary(session_id: str, db_path: str) -> tuple[str | None, int]:
    """Return ``(summary, summary_offset)`` from the session row."""
    if not db_path:
        return None, 0
    db = await get_session(db_path)
    try:
        row = (
            await db.execute(
                select(Session.summary, Session.summary_offset).where(
                    Session.session_id == session_id
                )
            )
        ).first()
        return (row[0], row[1]) if row else (None, 0)
    finally:
        await db.close()


async def _save_session_summary(
    session_id: str, db_path: str, summary: str, summary_offset: int
) -> None:
    """Persist updated summary and offset."""
    if not db_path:
        return
    db = await get_session(db_path)
    try:
        await db.execute(
            update(Session)
            .where(Session.session_id == session_id)
            .values(summary=summary, summary_offset=summary_offset)
        )
        await db.commit()
    finally:
        await db.close()


def _determine_recent_keep(messages: list, min_keep: int = 2) -> list:
    """Return the tail slice of messages that should be kept out of summarisation.

    Always keeps at least *min_keep* messages.  If the tail contains
    ``ToolMessage``s, walks backwards to also include the ``AIMessage`` that
    initiated those tool calls — otherwise the LLM sees orphaned tool results.
    """
    if len(messages) <= min_keep:
        return list(messages)

    tail = list(messages[-min_keep:])
    idx = len(messages) - min_keep - 1

    # Walk backwards while the preceding message is a ToolMessage
    while idx >= 0 and isinstance(messages[idx], ToolMessage):
        tail.insert(0, messages[idx])
        idx -= 1

    # If we stopped at an AIMessage with tool_calls, include it too
    if idx >= 0 and isinstance(messages[idx], AIMessage) and messages[idx].tool_calls:
        tail.insert(0, messages[idx])

    return tail


async def _summarize_messages(
    messages: list, previous_summary: str | None, fast_model: BaseChatModel
) -> str | None:
    """Call the fast model to produce a cumulative summary."""
    # Build context: previous summary + recent non-system messages
    non_system = filter_messages(messages, include_types=[HumanMessage, AIMessage, ToolMessage])

    parts = []
    if previous_summary:
        parts.append(f"Previous summary:\n{previous_summary}\n")
    parts.append("New messages:\n")
    for msg in non_system:
        parts.append(f"[{msg.type}] {msg.content}")

    result = await fast_model.ainvoke(
        [SystemMessage(content=_SUMMARIZE_PROMPT), HumanMessage(content="\n".join(parts))]
    )
    return result.content


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------


async def summarize_node(state: AgentState, config: RunnableConfig) -> dict:
    """Context-aware summarisation — runs before every agent call.

    Reads the session summary from the DB, estimates token usage, and if
    it exceeds 80 % of the model's context window, calls the fast model to
    produce a new cumulative summary.

    ``summary_offset`` is stored in graph state (checkpoint-managed) so that
    the offset and the messages it indexes are committed atomically.  The
    summary *text* is still persisted to the session table for cross-session
    recovery.

    Returns a dict that LangGraph merges into ``AgentState``:
    ``effective_messages`` — the compressed message list the agent should
    send to the LLM.
    """
    configurable: dict[str, Any] = config.get("configurable", {})

    session_id: str = configurable.get("session_id", state.get("session_id", ""))
    db_path: str = configurable.get("sqlite_db_path", "")
    llm_manager: LLMProviderManager = configurable.get("llm_manager")

    # ── Task boundary: reset iteration counter ──────────────────────────
    # When the previous task ended (completed/failed), after_agent routes
    # to __end__.  The next message starts a fresh task, so reset the
    # counter so the new task gets its full quota of MAX_AGENT_ITERATIONS.
    prev_status = state.get("status", "")
    agent_iteration = 0 if prev_status in ("completed", "failed") else state.get("agent_iteration", 0)

    ctx_window = llm_manager.base_model_context_window if llm_manager else 128000
    threshold = int(ctx_window * _SUMMARIZE_THRESHOLD)

    # summary_offset lives in checkpoint state; fall back to DB on first run
    # (e.g. after a fresh deployment where state has no offset yet).
    summary_offset = state.get("summary_offset", 0)
    session_summary, db_offset = await _load_session_summary(session_id, db_path)
    if summary_offset == 0 and db_offset > 0:
        summary_offset = db_offset

    all_messages = state["messages"]
    effective_messages = all_messages[summary_offset:]

    token_count = count_tokens_approximately(effective_messages)

    # Account for the system prompt and summary that will be prepended
    # by this node in the final_messages assembly below.
    _overhead = len(AGENT_SYSTEM_PROMPT) // 4 + (len(session_summary) // 4 if session_summary else 0)

    if token_count + _overhead > threshold and len(effective_messages) > 2:
        logger.info(
            "summarize_node_triggered",
            session_id=session_id,
            token_count=token_count,
            message_count=len(effective_messages),
        )
        # Keep recent messages — include the AIMessage that initiated
        # tool calls so ToolMessages are not orphaned.
        recent_keep = _determine_recent_keep(effective_messages)
        to_summarize = effective_messages[: len(effective_messages) - len(recent_keep)]

        fast_model = llm_manager.fast_model
        new_summary = None
        if fast_model:
            new_summary = await _summarize_messages(to_summarize, session_summary, fast_model)

        if new_summary:
            session_summary = new_summary
            summary_offset = len(all_messages) - len(recent_keep)
            # Persist summary text to DB for cross-session recovery;
            # summary_offset is committed with the checkpoint below.
            await _save_session_summary(session_id, db_path, session_summary, summary_offset)
            effective_messages = recent_keep

    # Build the final message list the agent will send to the LLM
    final_messages: list = [SystemMessage(content=AGENT_SYSTEM_PROMPT)]
    if session_summary:
        final_messages.append(SystemMessage(content=f"Conversation summary:\n{session_summary}"))
    final_messages.extend(effective_messages)

    return {
        "effective_messages": final_messages,
        "summary_offset": summary_offset,
        "agent_iteration": agent_iteration,
    }
