"""Routing functions for the tool-calling agent graph.

These conditional-edge callbacks determine which node runs next based on
the current state.

Graph flow::

    summarize ──→ agent ── (no tool_calls) ──→ END
                     │
                     └── (has tool_calls) ──→ precheck ──→ confirm ──(needs more)──→ confirm
                                                                 │
                                                                 └──(all done)──→ Send(tools, call_1) ─┐
                                                                                 Send(tools, call_2) ─┼→ summarize
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
        ``"precheck"`` if there are pending tool calls to process.
        ``"__end__"`` if the agent produced a final answer or failed.
    """
    if state.get("status") in ("completed", "failed"):
        return "__end__"

    pending = state.get("pending_tool_calls")
    if not pending:
        return "__end__"

    return "precheck"


def after_confirm(state: AgentState) -> str | list[Send]:
    """Route after the confirm node.

    Merges ``allowed_tool_calls`` (from ``precheck_node``) with
    ``confirmed_tool_calls`` (user-approved via ``confirm_node``) and
    fans them out to the tools node.

    If there are remaining tools that need confirmation, loops back to
    the confirm node for another interrupt cycle.

    Returns:
        ``"confirm"`` — loop back to confirm_node for remaining tools.
        ``[Send("tools", ...), ...]`` — fan out all ready tool calls as
        independent branches (one per tool call).
        ``[]`` — empty list signals END (all tools were blocked/rejected).
    """
    needs_confirmation = state.get("needs_confirmation_tool_calls") or []
    if needs_confirmation:
        return "confirm"

    # Merge allowed and confirmed tool calls
    allowed = state.get("allowed_tool_calls") or []
    confirmed = state.get("confirmed_tool_calls") or []
    all_calls = allowed + confirmed
    if not all_calls:
        # No tools to execute — route back to agent so it can process
        # rejection messages and provide a response to the user
        return "summarize"

    return [
        Send("tools", {"confirmed_tool_calls": [tc]})
        for tc in all_calls
    ]


def after_tools(state: AgentState) -> str:
    """Decide what happens after the tools node.

    Tool results always go back to the agent for synthesis — the LLM may
    produce a final answer or request additional tool calls.
    """
    if state.get("status") == "failed":
        return "__end__"

    return "summarize"
