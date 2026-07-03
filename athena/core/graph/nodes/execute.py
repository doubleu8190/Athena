"""Execute node — the heart of subtask execution.

Each invocation handles ONE subtask (dispatched by ``route_to_ready_subtasks``
via ``Send("execute", {"subtask": subtask})``).  The pipeline:

1. Template rendering (Jinja2 Sandbox) — reuses ``athena.core.sandbox.render_args``
2. Harness pre-check — reuses ``HarnessEngine.pre_check()``
3. Risk-based confirmation via ``interrupt()`` (LangGraph human-in-the-loop)
4. MCP tool call via ``MCPClient.call_tool()``
5. Output normalization via ``athena.core.sandbox.normalize_output``

This node is stateless with respect to *which* subtask it runs — all context
comes from the ``subtask`` key injected by ``Send()``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langgraph.types import RunnableConfig, interrupt

from athena.core.graph.state import ExecutionState
from athena.core.sandbox import render_args, normalize_output
from athena.logging_config import bind_context, get_logger

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────

MAX_RETRIES = 5
RETRY_MIN_WAIT = 1
RETRY_MAX_WAIT = 60


async def execute_node(
    state: ExecutionState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Execute a single subtask.

    The subtask dict is injected into state by ``Send("execute", …)``
    via the fanout routing function.  It contains the full SubtaskDef
    fields: step, intent, tool_name, args, depends_on, critical,
    on_failure, fallback_tool.
    """
    subtask = state.get("subtask")
    if not subtask:
        logger.warning("execute_node_called_without_subtask")
        return {}

    # Check for fallback execution (retry with different tool)
    is_fallback = state.get("subtask_result", {}).get("status") == "fallback"
    if is_fallback:
        fallback_info = state["subtask_result"].get("fallback_tool", {})
        if fallback_info and isinstance(fallback_info, dict):
            subtask = {
                **subtask,
                "tool_name": fallback_info.get("tool_name", subtask["tool_name"]),
                "args": fallback_info.get("args", subtask["args"]),
            }

    cfg = config["configurable"]
    harness = cfg["harness_engine"]
    mcp_client = cfg["mcp_client"]
    tool_registry = cfg["tool_registry"]

    step = subtask["step"]
    tool_name = subtask["tool_name"]
    log = bind_context(step=step, tool=tool_name)

    # ── 1. Template rendering ────────────────────────────────────────
    try:
        # Build a mini task-context from current state
        task_ctx = _build_task_context(state)
        rendered_args = render_args(subtask.get("args", {}), task_ctx)
    except Exception as e:
        log.warning("template_render_failed", error=str(e))
        return {
            "subtask_result": {
                "step": step,
                "status": "failed",
                "error": f"Template render failed: {e}",
                "on_failure": subtask.get("on_failure", "abort"),
                "critical": subtask.get("critical", True),
                "fallback_tool": subtask.get("fallback_tool"),
                "rendered_args": {},
            }
        }

    log.info("executing_subtask", intent=subtask.get("intent", ""), args=rendered_args)

    # ── 2. Harness pre-check on rendered args ─────────────────────────
    harness_result = await harness.pre_check(tool_name, rendered_args)
    if not harness_result.allowed:
        log.warning("harness_blocked", reason=harness_result.reason)
        return {
            "subtask_result": {
                "step": step,
                "status": "blocked",
                "error": f"Harness blocked: {harness_result.reason}",
                "rendered_args": rendered_args,
            }
        }

    # ── 3. Confirmation (human-in-the-loop) ───────────────────────────
    if harness_result.requires_confirmation:

        decision = interrupt({
            "type": "confirmation_required",
            "step": step,
            "tool_name": tool_name,
            "intent": subtask.get("intent", ""),
            "risk_level": harness_result.risk_level.value,
            "args": rendered_args,
            "cooling_off_seconds": harness_result.cooling_off_seconds,
        })

        if decision != "approved":
            log.info("user_rejected", step=step)
            return {
                "subtask_result": {
                    "step": step,
                    "status": "user_rejected",
                    "error": "User rejected the operation",
                    "rendered_args": rendered_args,
                },
                "status": "executing",
            }

    # ── 4. Retry handling ─────────────────────────────────────────────
    retry_counts = state.get("retry_counts", {})
    retry_count = retry_counts.get(step, 0)

    # If we're re-entering after a retry, apply backoff
    if retry_count > 0:
        wait = min(RETRY_MIN_WAIT * (2 ** (retry_count - 1)), RETRY_MAX_WAIT)
        log.info("retrying_with_backoff", attempt=retry_count, wait_seconds=wait)
        await asyncio.sleep(wait)

    # ── 5. MCP tool call ──────────────────────────────────────────────
    try:
        # Resolve server_id for the tool
        tool = tool_registry.get_tool_by_name(tool_name)
        server_id = tool.source_server_id if tool else "builtin-core"

        result = await mcp_client.call_tool(
            server_id=server_id,
            tool_name=tool_name,
            arguments=rendered_args,
        )

        if result.success:
            normalized = normalize_output(result.content)
            log.info("subtask_success", step=step)
            return {
                "subtask_result": {
                    "step": step,
                    "status": "success",
                    "output": result.content,
                    "normalized_output": normalized,
                    "tool_name": tool_name,
                    "rendered_args": rendered_args,
                },
                "status": "executing",
            }
        else:
            log.warning("subtask_tool_failed", error=result.error)
            new_retry_counts = dict(retry_counts)
            new_retry_counts[step] = retry_count + 1
            return {
                "subtask_result": {
                    "step": step,
                    "status": "failed",
                    "error": result.error or "Tool call returned failure",
                    "on_failure": subtask.get("on_failure", "abort"),
                    "critical": subtask.get("critical", True),
                    "fallback_tool": subtask.get("fallback_tool"),
                    "rendered_args": rendered_args,
                    "tool_name": tool_name,
                },
                "retry_counts": new_retry_counts,
                "status": "executing",
            }

    except Exception as e:
        log.error("subtask_exception", error=str(e))
        new_retry_counts = dict(retry_counts)
        new_retry_counts[step] = retry_count + 1
        return {
            "subtask_result": {
                "step": step,
                "status": "failed",
                "error": str(e),
                "on_failure": subtask.get("on_failure", "abort"),
                "critical": subtask.get("critical", True),
                "fallback_tool": subtask.get("fallback_tool"),
                "rendered_args": rendered_args,
                "tool_name": tool_name,
            },
            "retry_counts": new_retry_counts,
            "status": "executing",
        }


def _build_task_context(state: dict[str, Any]):
    """Build a minimal TaskContext-like object for template rendering.

    The sandbox module expects a TaskContext with ``step_outputs`` and
    ``completed_steps`` attributes.  We create a lightweight stand-in
    from the current ExecutionState.
    """
    from athena.core.task_context import TaskContext

    return TaskContext(
        task_id=state.get("task_id", ""),
        session_id=state.get("session_id", ""),
        user_id=state.get("user_id", ""),
        channel=state.get("channel", "web"),
        step_outputs=state.get("step_outputs", {}),
        completed_steps=state.get("completed_steps", []),
    )
