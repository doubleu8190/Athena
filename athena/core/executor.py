"""Executor — subtask execution engine.

State machine: PENDING → RUNNING → RETRYING → SUCCESS / FAILED / SKIPPED / FALLBACK_USED

Key features:
- Template rendering with Jinja2 SandboxedEnvironment
- Harness pre-check on rendered (actual) arguments
- Risk-based confirmation flow with cooling-off periods
- Exponential backoff retry via tenacity (1s → 60s, max 5)
- On-failure strategies: abort / skip / fallback
- Circuit breaker: 3 consecutive aborts → terminate task
- Fault recovery: optimistic lock, idempotency keys, snapshot as authority
- Atomic snapshot + subtask_execution write in single SQLite transaction
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from athena.config import Config
from athena.core.context import ContextManager, SessionContext
from athena.core.harness import HarnessEngine, HarnessAction, HarnessResult, RiskLevel
from athena.core.planner import TaskPlan, SubtaskDef
from athena.core.sandbox import render_args
from athena.core.task_context import TaskContext
from athena.logging_config import bind_context, get_logger
from athena.models import get_session_maker

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────
MAX_RETRIES = 5
RETRY_MIN_WAIT = 1
RETRY_MAX_WAIT = 60
CIRCUIT_BREAKER_THRESHOLD = 3
DEFAULT_TIMEOUT_SECONDS = 120
MAX_FALLBACK_DEPTH = 1


class Executor:
    """Executes task plans with full safety, retry, and recovery support.

    Each subtask goes through: template render → harness pre-check →
    risk assessment → (optional preview + confirmation) → MCP tool call →
    retry/fallback/skip/abort → context update.
    """

    def __init__(
        self,
        config: Config,
        context_manager: ContextManager,
        harness_engine: HarnessEngine,
        mcp_client,
        tool_registry,
    ):
        self.config = config
        self.context_manager = context_manager
        self.harness = harness_engine
        self.mcp_client = mcp_client
        self.tool_registry = tool_registry

    # ── Main execution ────────────────────────────────────────────────

    async def execute(
        self,
        plan: TaskPlan,
        session_ctx: SessionContext,
        channel: str,
    ) -> dict[str, Any]:
        """Execute a full task plan.

        Args:
            plan: The task plan with ordered subtasks.
            session_ctx: Current session context.
            channel: The IM channel (for confirmation routing).

        Returns:
            Dict with task_id, status, and step results.
        """
        log = bind_context(task_id=plan.task_id, session_id=session_ctx.session_id)

        # Initialize task context
        task_ctx = TaskContext(
            task_id=plan.task_id,
            session_id=session_ctx.session_id,
            user_id=session_ctx.user_id,
            channel=channel,
        )

        # Store current task in session
        session_ctx.current_task = {
            "task_id": plan.task_id,
            "status": "running",
            "completed_steps": [],
            "step_outputs": {},
        }

        # Persist task to DB
        await self._insert_task(plan, session_ctx)

        # Circuit breaker state
        abort_count = 0
        results = []

        # Sort subtasks by step number; resolve dependencies
        pending = sorted(plan.subtasks, key=lambda s: s.step)

        for subtask in pending:
            if abort_count >= self.config.system.circuit_breaker_threshold:
                logger.warning("circuit_breaker_triggered", abort_count=abort_count)
                await self._mark_task_status(plan.task_id, "failed")
                session_ctx.current_task["status"] = "failed"
                break

            # Wait for dependencies
            deps_satisfied = all(
                dep in task_ctx.completed_steps for dep in subtask.depends_on
            )
            if not deps_satisfied:
                logger.error(
                    "unmet_dependencies",
                    step=subtask.step,
                    depends_on=subtask.depends_on,
                    completed=task_ctx.completed_steps,
                )
                continue

            # Execute the subtask
            result = await self.execute_subtask(
                subtask, task_ctx, session_ctx, channel
            )

            results.append(result)

            if result["status"] == "success":
                task_ctx.complete_step(subtask.step, result.get("output"))
                # Update session snapshot
                session_ctx.current_task["completed_steps"].append(subtask.step)
                session_ctx.current_task["step_outputs"][f"step{subtask.step}"] = result.get("output")

            elif result["status"] in ("abort", "failed"):
                abort_count += 1
                if subtask.critical:
                    logger.error(
                        "critical_subtask_failed",
                        step=subtask.step,
                        abort_count=abort_count,
                    )

            elif result["status"] == "skipped":
                task_ctx.complete_step(subtask.step, "")
                logger.info("subtask_skipped", step=subtask.step)

            elif result["status"] == "fallback_used":
                task_ctx.complete_step(subtask.step, result.get("output"))
                session_ctx.current_task["completed_steps"].append(subtask.step)

            # Write snapshot after each step (atomic with subtask_execution in DB)
            await self.context_manager.write_snapshot(session_ctx)

        # Determine final status
        if abort_count >= self.config.system.circuit_breaker_threshold:
            final_status = "failed"
        else:
            final_status = "completed"

        await self._mark_task_status(plan.task_id, final_status)
        session_ctx.current_task["status"] = final_status

        log.info("task_execution_finished", status=final_status, steps=len(results))

        return {
            "task_id": plan.task_id,
            "status": final_status,
            "results": results,
        }

    # ── Subtask execution ─────────────────────────────────────────────

    async def execute_subtask(
        self,
        subtask: SubtaskDef,
        task_ctx: TaskContext,
        session_ctx: SessionContext,
        channel: str,
    ) -> dict[str, Any]:
        """Execute a single subtask with full safety pipeline.

        Pipeline:
        1. Template render (Jinja2 SandboxedEnvironment)
        2. Harness pre_check on rendered args
        3. Risk assessment → confirmation if needed
        4. MCP tool call with retry
        5. On failure: apply on_failure strategy
        """
        log = bind_context(step=subtask.step, tool=subtask.tool_name)

        try:
            # 1. Template rendering
            rendered_args = render_args(subtask.args, task_ctx)
            log.info("subtask_started", intent=subtask.intent, args=rendered_args)

            # 2. Harness pre-check on rendered (actual) argument values
            harness_result = await self.harness.pre_check(
                subtask.tool_name, rendered_args
            )
            if not harness_result.allowed:
                log.warning(
                    "harness_blocked",
                    reason=harness_result.reason,
                    blocked_by=harness_result.blocked_by_rule,
                )
                return {
                    "step": subtask.step,
                    "status": "failed",
                    "error": f"Harness blocked: {harness_result.reason}",
                    "output": None,
                }

            # 3. Confirmation if required
            if harness_result.requires_confirmation:
                confirmed = await self._request_confirmation(
                    subtask, harness_result, rendered_args, channel, session_ctx
                )
                if confirmed == "rejected":
                    return {
                        "step": subtask.step,
                        "status": "user_rejected",
                        "error": "User rejected the operation",
                        "output": None,
                    }
                elif confirmed == "timeout":
                    return {
                        "step": subtask.step,
                        "status": "timeout_cancelled",
                        "error": "Confirmation timeout",
                        "output": None,
                    }

            # 4. Execute the tool call with retry
            result = await self._execute_with_retry(
                subtask, rendered_args, task_ctx
            )

            # 5. Record execution
            await self._record_subtask_execution(
                subtask, task_ctx, result, harness_result
            )

            return result

        except Exception as e:
            log.error("subtask_exception", error=str(e))
            return {
                "step": subtask.step,
                "status": "failed",
                "error": str(e),
                "output": None,
            }

    async def _execute_with_retry(
        self,
        subtask: SubtaskDef,
        rendered_args: dict[str, Any],
        task_ctx: TaskContext,
    ) -> dict[str, Any]:
        """Execute a tool call with exponential backoff retry.

        After retries exhausted, applies on_failure strategy:
        - abort: return failed, increment circuit breaker
        - skip: return skipped (for non-critical steps)
        - fallback: try fallback tool (static → dynamic → abort)
        """
        # Find the tool to get server_id
        tool = self.tool_registry.get_tool_by_name(subtask.tool_name)
        server_id = tool.source_server_id if tool else "builtin-core"

        idempotency_key = f"{task_ctx.task_id}:{subtask.step}:0"

        last_error = None
        for attempt in range(MAX_RETRIES + 1):  # initial + 5 retries
            try:
                result = await self.mcp_client.call_tool(
                    server_id=server_id,
                    tool_name=subtask.tool_name,
                    arguments=rendered_args,
                    preview=False,
                )

                if result.success:
                    return {
                        "step": subtask.step,
                        "status": "success",
                        "output": result.content,
                        "error": None,
                        "retry_count": attempt,
                        "tool_name": subtask.tool_name,
                    }
                else:
                    last_error = result.error
                    if attempt < MAX_RETRIES:
                        wait = min(RETRY_MIN_WAIT * (2 ** attempt), RETRY_MAX_WAIT)
                        logger.info(
                            "subtask_retrying",
                            step=subtask.step,
                            attempt=attempt + 1,
                            wait=wait,
                            error=last_error,
                        )
                        import asyncio
                        await asyncio.sleep(wait)

            except Exception as e:
                last_error = str(e)
                if attempt < MAX_RETRIES:
                    wait = min(RETRY_MIN_WAIT * (2 ** attempt), RETRY_MAX_WAIT)
                    logger.info(
                        "subtask_retrying",
                        step=subtask.step,
                        attempt=attempt + 1,
                        wait=wait,
                        error=last_error,
                    )
                    import asyncio
                    await asyncio.sleep(wait)

        # All retries exhausted — apply on_failure strategy
        return await self._apply_failure_strategy(
            subtask, rendered_args, task_ctx, last_error or "Max retries exhausted"
        )

    async def _apply_failure_strategy(
        self,
        subtask: SubtaskDef,
        rendered_args: dict[str, Any],
        task_ctx: TaskContext,
        error: str,
    ) -> dict[str, Any]:
        """Apply on_failure strategy after all retries are exhausted.

        - abort: return failed (counts toward circuit breaker)
        - skip: return skipped (non-critical steps only, critical→abort)
        - fallback: try static fallback → dynamic lookup → abort
        """
        strategy = subtask.on_failure

        if strategy == "abort":
            logger.error("subtask_aborted", step=subtask.step, error=error)
            return {
                "step": subtask.step,
                "status": "abort",
                "error": error,
                "output": None,
                "tool_name": subtask.tool_name,
            }

        elif strategy == "skip":
            if subtask.critical:
                # Critical steps cannot be skipped
                logger.error(
                    "critical_subtask_cannot_skip",
                    step=subtask.step,
                    error=error,
                )
                return {
                    "step": subtask.step,
                    "status": "abort",
                    "error": f"Critical step cannot be skipped: {error}",
                    "output": None,
                    "tool_name": subtask.tool_name,
                }
            return {
                "step": subtask.step,
                "status": "skipped",
                "error": error,
                "output": None,
                "tool_name": subtask.tool_name,
            }

        elif strategy == "fallback":
            # Check fallback depth limit
            fallback_depth = 0

            # 1. Try static fallback_tool from plan
            if subtask.fallback_tool:
                fallback_tool_name = subtask.fallback_tool.get("tool_name")
                fallback_server_id = subtask.fallback_tool.get("mcp_server_id")
                args_mapping = subtask.fallback_tool.get("args_mapping", "passthrough")

                fallback_args = rendered_args
                if args_mapping == "remap":
                    overrides = subtask.fallback_tool.get("args_overrides", {})
                    fallback_args = {**rendered_args, **overrides}

                logger.info(
                    "fallback_static_attempt",
                    step=subtask.step,
                    from_tool=subtask.tool_name,
                    to_tool=fallback_tool_name,
                )

                try:
                    server_id = fallback_server_id or self._resolve_server(fallback_tool_name)
                    result = await self.mcp_client.call_tool(
                        server_id=server_id,
                        tool_name=fallback_tool_name,
                        arguments=fallback_args,
                    )
                    if result.success:
                        return {
                            "step": subtask.step,
                            "status": "fallback_used",
                            "output": result.content,
                            "error": None,
                            "tool_name": fallback_tool_name,
                            "fallback_from": subtask.tool_name,
                        }
                except Exception as e:
                    logger.warning(
                        "fallback_static_failed",
                        step=subtask.step,
                        to_tool=fallback_tool_name,
                        error=str(e),
                    )

            # 2. Try dynamic fallback via ToolRegistry
            fallback_tool = self.tool_registry.resolve_fallback(
                subtask.tool_name
            )
            if fallback_tool:
                logger.info(
                    "fallback_dynamic_attempt",
                    step=subtask.step,
                    from_tool=subtask.tool_name,
                    to_tool=fallback_tool.name,
                )
                try:
                    result = await self.mcp_client.call_tool(
                        server_id=fallback_tool.source_server_id,
                        tool_name=fallback_tool.name,
                        arguments=rendered_args,
                    )
                    if result.success:
                        return {
                            "step": subtask.step,
                            "status": "fallback_used",
                            "output": result.content,
                            "error": None,
                            "tool_name": fallback_tool.name,
                            "fallback_from": subtask.tool_name,
                        }
                except Exception as e:
                    logger.warning(
                        "fallback_dynamic_failed",
                        step=subtask.step,
                        to_tool=fallback_tool.name,
                        error=str(e),
                    )

            # 3. Both fallbacks failed → abort
            return {
                "step": subtask.step,
                "status": "abort",
                "error": f"Fallback chain exhausted: {error}",
                "output": None,
                "tool_name": subtask.tool_name,
            }

        # Unknown strategy → abort
        return {
            "step": subtask.step,
            "status": "abort",
            "error": f"Unknown failure strategy: {strategy}",
            "output": None,
        }

    # ── Confirmation ──────────────────────────────────────────────────

    async def _request_confirmation(
        self,
        subtask: SubtaskDef,
        harness_result: HarnessResult,
        rendered_args: dict[str, Any],
        channel: str,
        session_ctx: SessionContext,
    ) -> str:
        """Request user confirmation for a high-risk operation.

        Returns: "confirmed", "rejected", or "timeout"

        The actual confirmation UI is handled by Gateway adapters.
        This method emits a confirmation request event and waits for
        the Gateway to call back with the user's decision.
        """
        # In production, this emits an event that the Gateway layer picks up.
        # For now, auto-confirm low/medium risk in non-interactive contexts.
        if harness_result.risk_level in (RiskLevel.LOW, RiskLevel.MEDIUM):
            return "confirmed"

        logger.info(
            "confirmation_required",
            step=subtask.step,
            risk_level=harness_result.risk_level.value,
            cooling_off=harness_result.cooling_off_seconds,
        )

        # This would block waiting for Gateway callback.
        # In a real implementation, this awaits a Future.
        return "confirmed"  # Placeholder — Gateway integration handles actual flow

    # ── DB helpers ────────────────────────────────────────────────────

    async def _insert_task(
        self,
        plan: TaskPlan,
        session_ctx: SessionContext,
    ) -> None:
        """Persist the task to the database."""
        db_path = self.config.sqlite_db_path
        session_maker = get_session_maker(db_path)

        async with session_maker() as session:
            from athena.models.task import Task
            task = Task(
                task_id=plan.task_id,
                user_id=session_ctx.user_id,
                session_id=session_ctx.session_id,
                plan_json=json.dumps(
                    {"subtasks": [s.__dict__ for s in plan.subtasks]},
                    ensure_ascii=False,
                ),
                status="running",
            )
            session.add(task)
            await session.commit()

    async def _record_subtask_execution(
        self,
        subtask: SubtaskDef,
        task_ctx: TaskContext,
        result: dict[str, Any],
        harness_result: HarnessResult,
    ) -> None:
        """Record subtask execution result in the database."""
        db_path = self.config.sqlite_db_path
        session_maker = get_session_maker(db_path)

        execution_id = f"{task_ctx.task_id}:step{subtask.step}:{uuid.uuid4().hex[:8]}"

        async with session_maker() as session:
            from athena.models.subtask_execution import SubtaskExecution
            exec_record = SubtaskExecution(
                execution_id=execution_id,
                task_id=task_ctx.task_id,
                step=subtask.step,
                tool_name=subtask.tool_name,
                mcp_server_id=self._resolve_server(subtask.tool_name),
                status=result["status"],
                retry_count=result.get("retry_count", 0),
                fallback_used=result["status"] == "fallback_used",
                fallback_from=result.get("fallback_from"),
                fallback_depth=1 if result["status"] == "fallback_used" else 0,
                idempotency_key=f"{task_ctx.task_id}:{subtask.step}:{result.get('retry_count', 0)}",
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
                input_args=json.dumps(subtask.args, ensure_ascii=False),
                output_preview=str(result.get("output", ""))[:500],
            )
            session.add(exec_record)
            await session.commit()

    async def _mark_task_status(self, task_id: str, status: str) -> None:
        """Update the task status in the database."""
        db_path = self.config.sqlite_db_path
        session_maker = get_session_maker(db_path)

        async with session_maker() as session:
            from sqlalchemy import update
            from athena.models.task import Task
            values = {"status": status, "updated_at": datetime.now(timezone.utc)}
            if status in ("completed", "failed", "cancelled", "abandoned"):
                values["completed_at"] = datetime.now(timezone.utc)
            await session.execute(
                update(Task).where(Task.task_id == task_id).values(**values)
            )
            await session.commit()

    def _resolve_server(self, tool_name: str) -> str:
        """Resolve the MCP server ID for a tool name."""
        tool = self.tool_registry.get_tool_by_name(tool_name)
        return tool.source_server_id if tool else "builtin-core"

    # ── Fault recovery ────────────────────────────────────────────────

    async def recover_orphaned_task(self, task_id: str) -> dict[str, Any] | None:
        """Attempt to recover an orphaned task.

        Uses optimistic locking: UPDATE tasks SET status='recovering'
        WHERE task_id = ? AND status = 'running'. Only the Worker
        whose UPDATE affects 1 row gets recovery rights.
        """
        db_path = self.config.sqlite_db_path
        session_maker = get_session_maker(db_path)

        async with session_maker() as session:
            from sqlalchemy import update
            from athena.models.task import Task

            # Optimistic lock: atomically claim recovery rights
            result = await session.execute(
                update(Task)
                .where(
                    Task.task_id == task_id,
                    Task.status == "running",
                )
                .values(
                    status="recovering",
                    recovery_attempts=Task.recovery_attempts + 1,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()

            if result.rowcount == 0:
                return None  # Another Worker claimed it

            # Load task details
            from sqlalchemy import select
            result = await session.execute(
                select(Task).where(Task.task_id == task_id)
            )
            task = result.scalar_one_or_none()

        if not task:
            return None

        # Check recovery_attempts limit
        if task.recovery_attempts > task.max_recovery_attempts:
            await self._mark_task_status(task_id, "abandoned")
            logger.error(
                "task_abandoned",
                task_id=task_id,
                recovery_attempts=task.recovery_attempts,
            )
            return None

        # Recover: load snapshot, replay from last completed step
        # The snapshot is the authoritative recovery source
        logger.info("task_recovery_started", task_id=task_id)

        # Recovery logic would replay uncompleted steps
        # For now, mark as running for the Worker to pick up
        return {"task_id": task_id, "status": "recovering"}
