"""Confirm node — human-in-the-loop confirmation for tool calls.

Sits between ``precheck`` and ``tools`` in the graph.  Processes tool calls
that **require confirmation** (as classified by ``precheck_node``) via a
state-based flow (no ``interrupt()``).

On each invocation, processes exactly one tool call and returns remaining
tools to the graph state.  The ``after_confirm`` router loops back to this
node until all tools are processed.

Graph flow::

    precheck → confirm ──(awaiting)──→ confirm (external handling)
                                                      ↓
                    confirm ──(needs more)──→ confirm ──(all done)──→ Send(tools, confirmed_1) ─┐
                                                                      Send(tools, confirmed_2) ─┼→ summarize
                                                                      Send(tools, confirmed_3) ─┘

Confirmation decisions arrive via ``state["_confirmation_results"]``
(dict mapping tool_call_id → "approved"|"rejected").  When no decision
exists for a pending tool, the node returns ``_awaiting_confirmation``
so the external handler can process it.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig

from athena.core.graph.agent_state import AgentState
from athena.logging_config import bind_context, get_logger

logger = get_logger(__name__)


async def confirm_node(
    state: AgentState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Confirm tool calls that require user approval.

    Reads ``needs_confirmation_tool_calls`` (set by ``precheck_node``) and
    processes tools sequentially using pre-provided decisions from
    ``_confirmation_results`` in the state.

    On each invocation, processes the first tool in the list:
    - If a decision exists in ``_confirmation_results`` → process it.
    - If no decision exists → return ``_awaiting_confirmation`` flag so the
      external handler can request user input.

    Returns a dict with:
    - ``messages``: rejection ToolMessages for rejected calls.
    - ``confirmed_tool_calls``: list of tool calls that passed confirmation.
    - ``needs_confirmation_tool_calls``: remaining tools needing confirmation.
    - ``_awaiting_confirmation``: flag set when a decision is needed.
    """
    needs_confirmation = state.get("needs_confirmation_tool_calls")
    if not needs_confirmation:
        return {
            "needs_confirmation_tool_calls": None,
            "confirmed_tool_calls": None,
        }

    cfg = config.get("configurable", {})
    session_id = cfg.get("thread_id", state.get("session_id", ""))
    log = bind_context(session_id=session_id, node="confirm_node")

    messages: list[ToolMessage] = []
    confirmed: list[dict[str, Any]] = list(state.get("confirmed_tool_calls") or [])
    decisions: dict[str, str] = state.get("_confirmation_results") or {}

    tc = needs_confirmation[0]
    tool_name = tc.get("name", "")
    tool_call_id = tc.get("id", "")
    tool_args = tc.get("arguments", {})

    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except json.JSONDecodeError:
            tool_args = {}

    risk_level = tc.get("_harness_risk_level", "medium")
    cooling_off = tc.get("_harness_cooling_off", 0)
    reason = tc.get("_harness_reason", "")

    # Check if we have a decision for this tool
    decision = decisions.get(tool_call_id)

    if decision is None:
        # No decision yet — signal the external handler
        log.info("confirm_awaiting_decision", tool=tool_name, risk=risk_level)
        return {
            "_awaiting_confirmation": [tc],
            "confirmed_tool_calls": confirmed or None,
            "needs_confirmation_tool_calls": needs_confirmation or None,
        }

    # Process with the provided decision
    if decision != "approved":
        log.info("user_rejected", tool=tool_name)
        messages.append(ToolMessage(
            content=json.dumps({"error": "User rejected the operation"}),
            tool_call_id=tool_call_id,
            name=tool_name,
        ))
    else:
        log.info("user_approved", tool=tool_name)
        confirmed.append(tc)

    remaining = needs_confirmation[1:]

    log.info(
        "confirm_done",
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        result=decision,
    )

    return {
        "messages": messages or [],
        "confirmed_tool_calls": confirmed or None,
        "needs_confirmation_tool_calls": remaining or None,
    }
