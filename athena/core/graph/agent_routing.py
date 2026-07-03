"""Routing functions for the tool-calling agent graph.

These conditional-edge callbacks determine which node runs next based on
the current state.
"""

from __future__ import annotations

from athena.core.graph.agent_state import AgentState

# ── Constants ────────────────────────────────────────────────────────────────

MAX_AGENT_ITERATIONS = 10
"""Hard limit on agent → tools → agent loops to prevent runaway chains."""


def after_agent(state: AgentState) -> str:
    """Decide what happens after the agent node.

    Returns:
        * ``"tools"`` — the LLM requested tool calls and we haven't exceeded
          the iteration limit
        * ``"__end__"`` — the LLM answered directly or we've hit the limit
    """
    if state.get("status") == "completed":
        return "__end__"

    if state.get("status") == "failed":
        return "__end__"

    if state.get("pending_tool_calls"):
        iteration = state.get("agent_iteration", 0)
        if iteration >= MAX_AGENT_ITERATIONS:
            return "__end__"
        return "tools"

    return "__end__"


def after_tools(state: AgentState) -> str:
    """Decide what happens after the tools node.

    Tool results always go back to the agent for synthesis — the LLM may
    produce a final answer or request additional tool calls.
    """
    # If the state is failed, stop
    if state.get("status") == "failed":
        return "__end__"

    return "agent"
