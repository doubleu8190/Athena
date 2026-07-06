"""Routing functions for the tool-calling agent graph.

These conditional-edge callbacks determine which node runs next based on
the current state.  Uses LangGraph's ``Send()`` to fan out individual
tool calls so each runs as an independent sub-node (prevents duplicate
execution when ``interrupt()`` is used for human-in-the-loop confirmation).
"""

from __future__ import annotations

from langgraph.types import Send

from athena.core.graph.agent_state import AgentState

# ── Constants ────────────────────────────────────────────────────────────────

MAX_AGENT_ITERATIONS = 10
"""Hard limit on agent → tools → agent loops to prevent runaway chains."""


def after_agent(state: AgentState) -> list[Send]:
    """Route after the agent node.

    Returns:
        * ``[Send("tools", ...), ...]`` — fan out tool calls as independent
          branches (one per tool call)
        * ``[]`` — empty list signals END to LangGraph
    """
    if state.get("status") in ("completed", "failed"):
        return []

    pending = state.get("pending_tool_calls")
    if not pending:
        return []

    iteration = state.get("agent_iteration", 0)
    if iteration >= MAX_AGENT_ITERATIONS:
        return []

    # Fan out: each tool call becomes its own tools_node invocation
    return [
        Send("tools", {"pending_tool_calls": [tc]})
        for tc in pending
    ]


def after_tools(state: AgentState) -> str:
    """Decide what happens after the tools node.

    Tool results always go back to the agent for synthesis — the LLM may
    produce a final answer or request additional tool calls.
    """
    if state.get("status") == "failed":
        return "__end__"

    return "agent"
