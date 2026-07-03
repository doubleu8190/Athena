"""Collect node — aggregates subtask execution results.

This node runs after each batch of parallel ``execute`` invocations
completes.  It reads ``subtask_result`` and merges the outcome into
``completed_steps``, ``failed_steps``, and ``step_outputs``.

The merged state then flows into ``after_collect`` which decides
whether to fan-out more subtasks, re-plan, or finalize.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import RunnableConfig

from athena.core.graph.state import ExecutionState
from athena.logging_config import get_logger

logger = get_logger(__name__)


async def collect_node(
    state: ExecutionState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Collect the result of one completed subtask execution.

    Each call processes ONE ``subtask_result`` (from a single ``Send``
    branch).  When multiple branches complete concurrently, LangGraph
    merges their outputs via the state reducers.

    Returns:
        Partial state update with ``completed_steps``, ``failed_steps``,
        and ``step_outputs`` updates (using reducer semantics).
    """
    result = state.get("subtask_result")
    if not result:
        return {}

    step = result.get("step")
    if step is None:
        return {}

    status = result.get("status", "unknown")
    log = logger.bind(step=step, status=status)

    update: dict[str, Any] = {}

    if status in ("success", "fallback_used"):
        step_key = f"step{step}"
        normalized = result.get("normalized_output", result.get("output"))
        update["completed_steps"] = [step]
        update["step_outputs"] = {step_key: normalized}
        log.info("step_collected_success")

    elif status == "skipped":
        update["completed_steps"] = [step]
        log.info("step_collected_skipped")

    elif status in ("blocked", "abort", "failed"):
        update["failed_steps"] = [step]
        log.warning("step_collected_failed", error=result.get("error", ""))

    elif status == "user_rejected":
        update["failed_steps"] = [step]
        log.info("step_collected_rejected")

    else:
        log.warning("step_collected_unknown_status")

    # Clear subtask_result for the next batch
    update["subtask_result"] = None

    return update
