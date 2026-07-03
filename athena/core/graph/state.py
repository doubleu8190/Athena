"""ExecutionState — TypedDict with LangGraph reducer annotations.

The state schema for the unified plan+execute graph.  Only JSON-serializable
fields live here; live objects (SessionContext, MCPClient, HarnessEngine, …)
travel via `config["configurable"]`.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage


# ── Custom reducer: merge step_outputs dicts ─────────────────────────────

def _merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Reducer for step_outputs: shallow-merge right into left.

    LangGraph calls this with (current_value, update) for every node return
    that includes the annotated key.  We do a shallow merge so that a node
    returning ``{"step_outputs": {"step3": data}}`` only *adds* to the
    existing dict rather than replacing it.

    For completed_steps / failed_steps we use ``operator.add`` (list
    concatenation), which LangGraph supports natively.
    """
    merged = dict(left)
    merged.update(right)
    return merged


# ── State schema ─────────────────────────────────────────────────────────

class ExecutionState(TypedDict):
    """Typed state for the Athena execution graph.

    All values MUST be JSON-serializable for LangGraph checkpointing.
    Non-serializable dependencies (SessionContext, MCPClient, etc.) are
    passed through ``config["configurable"]``.
    """

    # ── Conversation ──
    messages: Annotated[list[BaseMessage], add_messages]
    """Full message history managed by LangGraph's add_messages reducer."""

    system_context: str | None
    """Dynamic system context rebuilt each invocation (conversation summary +
    RAG-injected memories).  Set by the caller in initial_state so the
    checkpointer can restore it; replaced wholesale on each new message."""

    # ── Planning ──
    task_plan: dict[str, Any] | None
    """Raw TaskPlan JSON dict with 'task_id' and 'subtasks' list."""

    plan_error: str | None
    """Non-null when the planner LLM call fails."""

    # ── Execution tracking ──
    step_outputs: Annotated[dict[str, Any], _merge_dicts]
    """Step outputs keyed by ``step<N>`` (e.g. ``{"step1": ..., "step2": ...}``).
    Each value is the *normalized* output (after normalize_output)."""

    completed_steps: Annotated[list[int], lambda left, right: left + right]
    """Steps that completed successfully or were skipped."""

    failed_steps: Annotated[list[int], lambda left, right: left + right]
    """Steps that failed (abort / blocked / exhausted retries)."""

    # ── Control flow (per-step, re-initialised by fanout) ──
    subtask_result: dict[str, Any] | None
    """The result of the currently-executing subtask.
    Written by ``execute_node`` / ``handle_failure_node`` and read by
    ``collect_node`` + the routing functions."""

    circuit_breaker_count: int
    """Consecutive abort count.  Trips at CIRCUIT_BREAKER_THRESHOLD (3)."""

    retry_counts: dict[int, int]
    """Per-step retry attempt counter.  Keyed by step number."""

    status: str
    """High-level graph status: ``planning`` | ``executing`` | ``confirming`` |
    ``completed`` | ``failed``."""

    # ── Session identifiers (serializable subset) ──
    session_id: str
    user_id: str
    channel: str
