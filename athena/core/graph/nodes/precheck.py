"""Precheck node — runs harness pre-check for all pending tool calls.

Sits between ``agent`` and ``confirm`` in the graph.  Classifies each
pending tool call into one of three categories:

* **blocked** — ``harness.pre_check`` denied the call → error ToolMessage.
* **needs_confirmation** — call requires user confirmation → forwarded to
  ``confirm_node`` for ``interrupt()`` handling.
* **allowed** — call passed all checks → ready for immediate execution.

This node contains **no** ``interrupt()`` calls, so it always runs to
completion in a single pass.  The harness check is executed exactly once
per tool call, eliminating the redundant re-execution that occurred when
``interrupt()`` lived inside the ``confirm_node`` for-loop.

Graph flow::

    agent → precheck → confirm → Send(tools, confirmed_1) ─┐
                                  Send(tools, confirmed_2) ─┼→ summarize
                                  Send(tools, confirmed_3) ─┘
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig

from athena.core.graph.agent_state import AgentState
from athena.logging_config import bind_context, get_logger

if __name__ != "__main__":
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from athena.core.harness import HarnessEngine

logger = get_logger(__name__)


async def precheck_node(
    state: AgentState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Run harness pre-check for every pending tool call.

    Classifies each call into blocked / needs_confirmation / allowed and
    writes the results into the corresponding state fields.  Does **not**
    call ``interrupt()`` — confirmation is handled by ``confirm_node``.

    Returns a dict with:
    - ``messages``: error ToolMessages for blocked calls.
    - ``allowed_tool_calls``: calls that passed without confirmation.
    - ``blocked_tool_calls``: calls blocked by the harness.
    - ``needs_confirmation_tool_calls``: calls requiring user confirmation.
    """
    pending = state.get("pending_tool_calls")
    if not pending:
        return {
            "allowed_tool_calls": None,
            "blocked_tool_calls": None,
            "needs_confirmation_tool_calls": None,
            "pending_tool_calls": None,
        }

    cfg = config.get("configurable", {})
    harness: HarnessEngine | None = cfg.get("harness_engine")
    if harness is None:
        from athena.core.harness import get_harness
        harness = await get_harness()
    session_id = cfg.get("session_id", "")

    log = bind_context(session_id=session_id, node="precheck_node")

    messages: list[ToolMessage] = []
    allowed: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    needs_confirmation: list[dict[str, Any]] = []

    for tc in pending:
        tool_name = tc.get("name", "")
        tool_args = tc.get("arguments", {})
        tool_call_id = tc.get("id", "")

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
            blocked.append(tc)
            continue

        if not harness_result.allowed:
            log.warning("harness_blocked", tool=tool_name, reason=harness_result.reason)
            messages.append(ToolMessage(
                content=json.dumps({"error": f"Operation blocked: {harness_result.reason}"}),
                tool_call_id=tool_call_id,
                name=tool_name,
            ))
            blocked.append(tc)
            continue

        if harness_result.requires_confirmation:
            log.info("confirm_required", tool=tool_name, risk=harness_result.risk_level.value)
            # Store harness metadata on the tool call for confirm_node to use
            tc["_harness_risk_level"] = harness_result.risk_level.value
            tc["_harness_cooling_off"] = harness_result.cooling_off_seconds
            tc["_harness_reason"] = getattr(harness_result, "reason", "")
            needs_confirmation.append(tc)
        else:
            allowed.append(tc)

    log.info(
        "precheck_done",
        total=len(pending),
        allowed=len(allowed),
        blocked=len(blocked),
        needs_confirmation=len(needs_confirmation),
    )

    return {
        "messages": messages if messages else [],
        "allowed_tool_calls": allowed or None,
        "blocked_tool_calls": blocked or None,
        "needs_confirmation_tool_calls": needs_confirmation or None,
        "pending_tool_calls": None,
    }
