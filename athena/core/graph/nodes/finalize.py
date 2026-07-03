"""Finalize node — persists results and marks the task complete.

This is the last node before END.  It:
1. Determines the final task status (completed / failed)
2. Persists the task result to the ``tasks`` table
3. Writes the session snapshot via ContextManager
4. Updates the session's current_task in Redis
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from langgraph.types import RunnableConfig

from athena.core.graph.state import ExecutionState
from athena.logging_config import get_logger

logger = get_logger(__name__)


async def finalize_node(
    state: ExecutionState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Finalize the task: write results and update session.

    Returns the final ``status`` ("completed" | "failed") which can be
    read by the SSE streaming layer.
    """
    cfg = config["configurable"]
    context_manager = cfg.get("context_manager")
    session_ctx = cfg.get("session_context")

    plan = state.get("task_plan", {})
    task_id = plan.get("task_id", "")

    # Determine final status
    breaker = state.get("circuit_breaker_count", 0)
    if breaker >= 3:
        final_status = "failed"
    elif state.get("failed_steps"):
        # Some failures but breaker didn't trip — partial success
        final_status = "completed"
    else:
        final_status = "completed"

    log = logger.bind(task_id=task_id, status=final_status)
    log.info("task_finalizing", completed=len(state.get("completed_steps", [])),
             failed=len(state.get("failed_steps", [])))

    # ── Persist task result to DB ─────────────────────────────────────
    try:
        await _persist_task_result(state, config, final_status)
    except Exception as e:
        log.error("persist_task_failed", error=str(e))

    # ── Write session snapshot ────────────────────────────────────────
    if context_manager and session_ctx:
        try:
            # Update session task state
            session_ctx.current_task = {
                "task_id": task_id,
                "status": final_status,
                "completed_steps": state.get("completed_steps", []),
                "step_outputs": state.get("step_outputs", {}),
            }
            await context_manager.write_snapshot(session_ctx)
        except Exception as e:
            log.error("snapshot_write_failed", error=str(e))

    return {
        "status": final_status,
    }


async def _persist_task_result(
    state: dict[str, Any],
    config: RunnableConfig,
    final_status: str,
) -> None:
    """Write task final status to the ``tasks`` table."""
    cfg = config["configurable"]
    context_manager = cfg.get("context_manager")

    if not context_manager:
        return

    db_path = context_manager.config.sqlite_db_path
    from athena.models import get_session_maker

    session_maker = get_session_maker(db_path)
    plan = state.get("task_plan", {})
    task_id = plan.get("task_id", "")

    if not task_id:
        return

    async with session_maker() as session:
        from sqlalchemy import update
        from athena.models.task import Task

        values = {
            "status": final_status,
            "updated_at": datetime.now(timezone.utc),
        }
        if final_status in ("completed", "failed", "cancelled", "abandoned"):
            values["completed_at"] = datetime.now(timezone.utc)

        await session.execute(
            update(Task)
            .where(Task.task_id == task_id)
            .values(**values)
        )
        await session.commit()
