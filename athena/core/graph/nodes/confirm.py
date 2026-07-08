"""Confirm node — sequential human-in-the-loop confirmation for tool calls.

Sits between ``agent`` and ``tools`` in the graph.  Processes each pending
tool call that requires confirmation via ``interrupt()`` **sequentially**,
so each tool is confirmed exactly once.

Graph flow::

    agent → confirm → Send(tools, confirmed_1) ─┐
                        Send(tools, confirmed_2) ─┼→ agent (loop)
                        Send(tools, confirmed_3) ─┘

``interrupt()`` is only called inside this node — never inside ``Send()``
branches — which prevents the duplicate-confirmation bug caused by
LangGraph re-evaluating conditional edges on resume.
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.types import RunnableConfig, interrupt
from langchain_core.messages import ToolMessage

from athena.core.graph.agent_state import AgentState
from athena.logging_config import bind_context, get_logger

if __name__ != "__main__":
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from athena.core.harness import HarnessEngine

logger = get_logger(__name__)


async def confirm_node(
    state: AgentState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Check each pending tool call and interrupt for confirmation if needed.

    For each tool call in ``pending_tool_calls``:
    - Runs the harness pre-check.
    - If blocked → emits an error ToolMessage.
    - If requires_confirmation → calls ``interrupt()`` and waits for user.
    - If allowed → passes through to confirmed list.

    Returns a dict with:
    - ``messages``: error/rejection ToolMessages for blocked/rejected calls.
    - ``confirmed_tool_calls``: list of tool calls that passed confirmation.
    - ``pending_tool_calls``: cleared (all processed).
    """
    pending = state.get("pending_tool_calls")
    if not pending:
        return {
            "confirmed_tool_calls": None,
            "pending_tool_calls": None,
        }

    cfg = config["configurable"]
    harness: HarnessEngine = cfg["harness_engine"]
    session_id = cfg["session_id"]

    log = bind_context(session_id=session_id, node="confirm_node")

    messages: list[ToolMessage] = []
    confirmed: list[dict[str, Any]] = []

    for tc in pending:
        tool_name = tc.get("name", "")
        tool_args = tc.get("arguments", {})
        tool_call_id = tc.get("id", "")

        # Ensure args is a dict
        if isinstance(tool_args, str):
            try:
                tool_args = json.loads(tool_args)
            except json.JSONDecodeError:
                tool_args = {}

        # ── Harness pre-check ─────────────────────────────────────────
        try:
            harness_result = await harness.pre_check(state, tool_name, tool_args)
        except Exception as e:
            log.warning("harness_check_exception", tool=tool_name, error=str(e))
            messages.append(ToolMessage(
                content=json.dumps({"error": f"Security check failed: {e}"}),
                tool_call_id=tool_call_id,
                name=tool_name,
            ))
            continue

        if not harness_result.allowed:
            log.warning("harness_blocked", tool=tool_name, reason=harness_result.reason)
            messages.append(ToolMessage(
                content=json.dumps({"error": f"Operation blocked: {harness_result.reason}"}),
                tool_call_id=tool_call_id,
                name=tool_name,
            ))
            continue

        # ── Confirmation (sequential interrupt) ───────────────────────
        if harness_result.requires_confirmation:
            log.info("confirm_required", tool=tool_name, risk=harness_result.risk_level.value)

            decision = interrupt({
                "type": "confirmation_required",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "args": tool_args,
                "risk_level": harness_result.risk_level.value,
                "cooling_off_seconds": harness_result.cooling_off_seconds,
                "reason": getattr(harness_result, "reason", ""),
            })

            if decision != "approved":
                log.info("user_rejected", tool=tool_name)
                messages.append(ToolMessage(
                    content=json.dumps({"error": "User rejected the operation"}),
                    tool_call_id=tool_call_id,
                    name=tool_name,
                ))
                continue

            log.info("user_approved", tool=tool_name)

        # ── Tool passed confirmation ──────────────────────────────────
        confirmed.append(tc)

    log.info(
        "confirm_done",
        total=len(pending),
        confirmed=len(confirmed),
        blocked=len(messages),
    )

    return {
        "messages": messages,
        "confirmed_tool_calls": confirmed if confirmed else None,
        "pending_tool_calls": None,
    }
