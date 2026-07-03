"""Conditional edge routing functions for the execution graph.

Each function inspects the current state and returns either a string
(next node name or ``"__end__"``) or a ``list[Send]`` to fan-out
parallel subtask executions.

The fan-out pattern:
- ``after_plan`` dispatches the initial batch of ready subtasks via Send
- ``after_collect`` dispatches the next batch after results are collected
- Each ``Send("execute", ...)`` targets the ``execute`` node
- ``execute`` routes to ``collect`` (success/skip) or loops (retry/fallback)
- ``collect`` is the sink where parallel branches converge

LangGraph 1.x semantics:
- When a conditional-edge function returns ``list[Send]``, the dispatcher node
  pauses and N parallel branches spawn.
- Each branch follows the NORMAL edges from the target node.
- When ALL branches reach the sink (``collect``), the sink runs with the
  merged state.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import Send

from athena.logging_config import get_logger

logger = get_logger(__name__)

# ── Constants (mirrored from executor.py) ────────────────────────────────

MAX_RETRIES = 5
CIRCUIT_BREAKER_THRESHOLD = 3


# ── Shared: build Send list for ready subtasks ──────────────────────────

def _build_sends(state: dict[str, Any]) -> list[Send]:
    """Build ``Send`` objects for all subtasks whose dependencies are met.

    A subtask is "ready" when:
    - It hasn't already completed or failed
    - All of its ``depends_on`` steps are in ``completed_steps``
    """
    plan = state.get("task_plan")
    if not plan:
        return []

    completed = set(state.get("completed_steps", []))
    failed = set(state.get("failed_steps", []))
    done = completed | failed

    sends: list[Send] = []
    for subtask in plan["subtasks"]:
        step = subtask["step"]
        if step in done:
            continue

        deps = subtask.get("depends_on", [])
        deps_satisfied = all(dep in completed for dep in deps)
        if deps_satisfied:
            sends.append(Send("execute", {"subtask": subtask}))

    if sends:
        logger.info(
            "fanout_dispatching",
            ready_count=len(sends),
            steps=[s.arg["subtask"]["step"] for s in sends],
        )
    return sends


# ── after_plan ───────────────────────────────────────────────────────────

def after_plan(state: dict[str, Any]) -> list[Send] | str:
    """Route after the plan node.

    Returns:
        ``list[Send]`` — fan-out to ready subtasks
        ``"__end__"`` — plan failed or empty
    """
    if state.get("plan_error"):
        logger.error("plan_failed", error=state["plan_error"])
        return "__end__"

    plan = state.get("task_plan")
    if not plan or not plan.get("subtasks"):
        logger.warning("plan_empty")
        return "__end__"

    sends = _build_sends(state)
    if sends:
        return sends
    return "__end__"


# ── after_execute ────────────────────────────────────────────────────────

def after_execute(state: dict[str, Any]) -> str:
    """Route after the execute node.

    Returns:
        ``"collect"`` — success, skipped, user_rejected
        ``"retry"`` — failed but retries remain (loops back to execute)
        ``"handle_failure"`` — all retries exhausted
    """
    result = state.get("subtask_result", {})
    status = result.get("status", "failed")

    if status in ("success", "skipped", "user_rejected"):
        return "collect"

    if status == "blocked":
        # Harness blocked — no point retrying
        return "handle_failure"

    if status in ("failed", "error"):
        step = result.get("step", 0)
        retry_count = state.get("retry_counts", {}).get(step, 0)
        if retry_count < MAX_RETRIES:
            return "retry"
        return "handle_failure"

    return "collect"


# ── after_failure ────────────────────────────────────────────────────────

def after_failure(state: dict[str, Any]) -> str:
    """Route after handle_failure.

    Returns:
        ``"collect"`` — abort / skip (step is done)
        ``"execute"`` — fallback (re-execute with different tool)
    """
    result = state.get("subtask_result", {})
    status = result.get("status", "")

    if status == "fallback":
        return "execute"

    return "collect"


# ── after_collect ────────────────────────────────────────────────────────

def after_collect(state: dict[str, Any]) -> list[Send] | str:
    """Decide the next phase after collecting step results.

    This is both the *sink* for parallel branches AND the dispatcher for
    the next batch.  It returns ``Send`` objects when more ready subtasks
    exist, or a string to route to ``plan`` / ``finalize`` / ``END``.

    Returns:
        ``list[Send]`` — fan-out next batch of ready subtasks
        ``"plan"`` — dynamic re-planning (failures exist, breaker not tripped)
        ``"finalize"`` — all done or circuit breaker tripped
        ``"__end__"`` — no plan (should not happen)
    """
    plan = state.get("task_plan")
    if not plan:
        return "__end__"

    total = len(plan["subtasks"])
    done = len(state.get("completed_steps", []))
    failed = len(state.get("failed_steps", []))

    # Circuit breaker tripped
    if state.get("circuit_breaker_count", 0) >= CIRCUIT_BREAKER_THRESHOLD:
        logger.warning("circuit_breaker_tripped")
        return "finalize"

    # All steps accounted for
    if done + failed >= total:
        return "finalize"

    # Failures exist but breaker hasn't tripped — trigger replan
    if failed > 0 and state.get("circuit_breaker_count", 0) == 0:
        logger.info("triggering_replan", failed=failed, done=done)
        return "plan"

    # Fan out next batch of ready subtasks
    sends = _build_sends(state)
    if sends:
        return sends

    # Nothing ready to run (should be handled above)
    logger.warning("after_collect_no_sends", done=done, failed=failed, total=total)
    return "finalize"
