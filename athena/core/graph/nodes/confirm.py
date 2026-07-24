"""Confirm node — sequential human-in-the-loop confirmation for tool calls.

Sits between ``precheck`` and ``tools`` in the graph.  Processes tool calls
that **require confirmation** (as classified by ``precheck_node``) via
``interrupt()`` **sequentially**, so each tool is confirmed exactly once.

On each invocation, processes exactly one tool call and returns remaining
tools to the graph state. The ``after_confirm`` router loops back to this
node until all tools are processed.

Graph flow::

    precheck → confirm ──(needs more)──→ confirm ──(all done)──→ Send(tools, confirmed_1) ─┐
                                                      Send(tools, confirmed_2) ─┼→ summarize
                                                      Send(tools, confirmed_3) ─┘

``interrupt()`` is only called inside this node — never inside ``Send()``
branches — which prevents the duplicate-confirmation bug caused by
LangGraph re-evaluating conditional edges on resume.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from athena.core.graph.agent_state import AgentState
from athena.logging_config import bind_context, get_logger

logger = get_logger(__name__)


async def confirm_node(
    state: AgentState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Confirm tool calls that require user approval.

    Reads ``needs_confirmation_tool_calls`` (set by ``precheck_node``) and
    processes tools sequentially via ``interrupt()``.

    On each invocation, processes the first tool in the list:
    - Calls ``interrupt()`` and waits for user decision.
    - If approved → appends to ``confirmed_tool_calls``.
    - If rejected → emits an error ToolMessage.
    - Returns remaining tools in ``needs_confirmation_tool_calls`` for
      the next cycle.

    Returns a dict with:
    - ``messages``: rejection ToolMessages for rejected calls.
    - ``confirmed_tool_calls``: list of tool calls that passed confirmation.
    - ``needs_confirmation_tool_calls``: remaining tools needing confirmation.
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
    # Carry forward any previously confirmed calls (from earlier interrupt cycles)
    confirmed: list[dict[str, Any]] = list(state.get("confirmed_tool_calls") or [])

    remaining_needs_confirmation = needs_confirmation[1:]
    tc = needs_confirmation[0]
    tool_name = tc.get("name", "")
    tool_args = tc.get("arguments", {})
    tool_call_id = tc.get("id", "")

    # Ensure args is a dict
    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except json.JSONDecodeError:
            tool_args = {}

    # ── Confirmation (sequential interrupt) ───────────────────────
    # Metadata was attached by precheck_node
    risk_level = tc.get("_harness_risk_level", "medium")
    cooling_off = tc.get("_harness_cooling_off", 0)
    reason = tc.get("_harness_reason", "")

    log.info("confirm_required", tool=tool_name, risk=risk_level)

    decision = interrupt({
        "type": "confirmation_required",
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "args": tool_args,
        "risk_level": risk_level,
        "cooling_off_seconds": cooling_off,
        "reason": reason,
    })

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

    log.info(
        "confirm_done",
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        result=decision,
    )

    return {
        "messages": messages or None,
        "confirmed_tool_calls": confirmed or None,
        "needs_confirmation_tool_calls": remaining_needs_confirmation or None,
    }
