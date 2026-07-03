"""Athena LangGraph orchestration layer.

Two graph implementations are available:

* **Agent graph** (``AgentState``, ``build_agent_graph``) — the current
  tool-calling agent that lets the LLM decide whether to answer directly or
  call tools, then synthesises a natural-language response.  This is the
  primary graph used in production.

* **Plan-execute graph** (``ExecutionState``, ``build_graph``) — the legacy
  plan-then-execute DAG that pre-generates a full TaskPlan before running
  subtasks.  Kept for backward compatibility but deprecated in favour of
  the agent graph.
"""

# ── Agent graph (current) ───────────────────────────────────────────────────
from athena.core.graph.agent_state import AgentState
from athena.core.graph.agent_graph import build_agent_graph

# ── Plan-execute graph (legacy) ─────────────────────────────────────────────
from athena.core.graph.state import ExecutionState, _merge_dicts
from athena.core.graph.graph import build_graph, create_checkpointer

__all__ = [
    # Current
    "AgentState",
    "build_agent_graph",
    # Legacy
    "ExecutionState",
    "build_graph",
    "create_checkpointer",
    "_merge_dicts",
]
