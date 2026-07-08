"""Routing functions for the tool-calling agent graph.

These conditional-edge callbacks determine which node runs next based on
the current state.

Graph flow::

    summarize ──→ agent ── (no tool_calls) ──→ END
                     │
                     └── (has tool_calls) ──→ confirm ──→ Send(tools, call_1) ─┐
                                                   Send(tools, call_2) ─┼→ summarize (loop)
                                                   Send(tools, call_3) ─┘

``interrupt()`` (human-in-the-loop) only happens inside ``confirm_node``
— never inside ``Send()`` branches — which prevents the duplicate
confirmation bug caused by LangGraph re-evaluating conditional edges on
resume.
"""

from __future__ import annotations

from langgraph.types import Send

from athena.core.graph.agent_state import AgentState

# ── Constants ────────────────────────────────────────────────────────────────

MAX_AGENT_ITERATIONS = 10
"""Hard limit on agent → tools → agent loops to prevent runaway chains."""


def after_agent(state: AgentState) -> str:
    """Route after the agent node.

    Returns:
        ``"confirm"`` if there are pending tool calls to process.
        ``"__end__"`` if the agent produced a final answer or failed.
    """
    if state.get("status") in ("completed", "failed"):
        return "__end__"

    pending = state.get("pending_tool_calls")
    if not pending:
        return "__end__"

    return "confirm"


def after_confirm(state: AgentState) -> list[Send]:
    """Route after the confirm node.

    Returns:
        ``[Send("tools", ...), ...]`` — fan out confirmed tool calls as
        independent branches (one per tool call).
        ``[]`` — empty list signals END (all tools were blocked/rejected).
    """
    confirmed = state.get("confirmed_tool_calls")
    if not confirmed:
        return []

    return [
        Send("tools", {"confirmed_tool_calls": [tc]})
        for tc in confirmed
    ]


def after_tools(state: AgentState) -> str:
    """Decide what happens after the tools node.

    Tool results always go back to the agent for synthesis — the LLM may
    produce a final answer or request additional tool calls.
    """
    if state.get("status") == "failed":
        return "__end__"

    return "summarize"
