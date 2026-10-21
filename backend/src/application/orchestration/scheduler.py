"""Worker 并行调度、状态收敛和重试用例。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from time import perf_counter

from domain.orchestration import (
    PlanRepository,
    PlanScheduler,
    ResultSynthesizer,
    TaskExecution,
    TaskResult,
    TaskStatus,
    WorkerExecutor,
)


class WorkerSchedulerService(PlanScheduler):
    """启动可运行 Worker，并在终态后推进 DAG。"""

    def __init__(
        self,
        repository: PlanRepository,
        worker: WorkerExecutor,
        *,
        synthesizer: ResultSynthesizer | None = None,
        events=None,
        max_retries: int = 1,
    ) -> None:
        self._repository = repository
        self._worker = worker
        self._synthesizer = synthesizer
        self._events = events
        self._max_retries = max_retries
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._retry_counts: dict[str, int] = {}
        self._results: dict[str, str] = {}
        self._task_started: dict[str, tuple[datetime, float]] = {}

    async def start_ready_tasks(self, plan_id: str) -> dict[str, object]:
        value = await self._repository.get(plan_id)
        if value is None:
            raise ValueError(f"plan not found: {plan_id}")
        plan, _ = value
        claimed = await self._repository.claim_ready_tasks(plan_id, plan.max_parallelism)
        for execution in claimed:
            key = f"{execution.task.task_id}:{execution.execution_generation}"
            self._task_started[key] = (datetime.now(timezone.utc), perf_counter())
            await self._publish_task(execution, "task.started", {"status": "running"})
            if key in self._tasks:
                continue
            task = asyncio.create_task(
                self._run(execution),
                name=f"athena-restructured-worker-{key}",
            )
            self._tasks[key] = task
            task.add_done_callback(lambda completed, key=key: self._tasks.pop(key, None))
        return {"plan_id": plan_id, "started_task_ids": [item.task.task_id for item in claimed]}

    async def _run(self, execution: TaskExecution) -> None:
        try:
            result = await self._worker.execute(execution)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = TaskResult(
                execution.task.task_id,
                TaskStatus.FAILED,
                error={"reason": "worker_failed", "message": str(exc) or type(exc).__name__},
                retryable=True,
            )

        if result.status == TaskStatus.WAITING_APPROVAL:
            await self._repository.set_task_waiting_approval(
                execution.task.task_id,
                execution.execution_generation,
                dict(result.approval_batch or {}),
            )
            await self._publish_task(execution, "task.waiting_approval", {"status": "waiting_approval"})
            return

        retries = self._retry_counts.get(execution.task.task_id, 0)
        retry_requested = (
            result.status in {TaskStatus.FAILED, TaskStatus.TIMED_OUT}
            and result.retryable
            and retries < self._max_retries
        )
        outcome = await self._repository.finish_task(
            execution.task.task_id,
            execution.execution_generation,
            status=result.status.value,
            output=dict(result.output) if result.status == TaskStatus.DONE else None,
            error=dict(result.error) if result.status != TaskStatus.DONE and result.error else None,
            finalize_plan=not retry_requested,
        )
        if not outcome.get("accepted"):
            return
        await self._publish_task(execution, "task.completed" if result.status == TaskStatus.DONE else "task.failed", {"status": result.status.value, "output": dict(result.output), "error": dict(result.error or {})})
        if retry_requested:
            self._retry_counts[execution.task.task_id] = retries + 1
            if await self._repository.retry_task(execution.task.task_id) is not None:
                await self.start_ready_tasks(execution.plan_id)
                return
            if outcome.get("plan_completed"):
                if await self._repository.finalize_plan(execution.plan_id) is not None:
                    await self._synthesize(execution.plan_id)
                return
        if outcome.get("plan_completed"):
            await self._synthesize(execution.plan_id)
        else:
            await self.start_ready_tasks(execution.plan_id)

    async def resume_worker(
        self, execution: TaskExecution, decisions: dict[str, str]
    ) -> dict[str, object]:
        if not await self._repository.set_task_running(
            execution.task.task_id, execution.execution_generation
        ):
            return {"accepted": False, "reason": "task_not_waiting_approval"}
        result = await self._worker.resume(execution, decisions)
        if result.status == TaskStatus.WAITING_APPROVAL:
            await self._repository.set_task_waiting_approval(
                execution.task.task_id,
                execution.execution_generation,
                dict(result.approval_batch or {}),
            )
            return {"accepted": True, "waiting_approval": True}
        outcome = await self._repository.finish_task(
            execution.task.task_id,
            execution.execution_generation,
            status=result.status.value,
            output=dict(result.output) if result.status == TaskStatus.DONE else None,
            error=dict(result.error) if result.status != TaskStatus.DONE and result.error else None,
        )
        if outcome.get("accepted") and not outcome.get("plan_completed"):
            await self.start_ready_tasks(execution.plan_id)
        elif outcome.get("plan_completed"):
            await self._synthesize(execution.plan_id)
        return outcome

    async def resume_task(
        self, task_id: str, decisions: dict[str, str]
    ) -> dict[str, object]:
        execution = await self._repository.get_task_execution(task_id)
        if execution is None:
            return {"accepted": False, "reason": "worker_not_found"}
        return await self.resume_worker(execution, decisions)

    async def cancel_run(self, run_id: str) -> int:
        return await self._worker.cancel(run_id)

    async def _synthesize(self, plan_id: str) -> None:
        value = await self._repository.get(plan_id)
        if value is None:
            return
        plan, _tasks = value
        if self._events is not None:
            from domain.events import ApplicationEvent, EventDurability, EventType

            await self._events.publish(ApplicationEvent(event_type=EventType.SYNTHESIS_STARTED, durability=EventDurability.DURABLE, session_id=plan.session_id or "", run_id=plan.run_id, payload={"activity_id": f"synthesis:{plan_id}", "activity_type": "synthesis", "name": "synthesis", "parent_activity_id": f"plan:{plan_id}", "plan_id": plan_id, "status": "running"}))
        results = await self._repository.get_results(plan_id)
        self._results[plan_id] = (
            await self._synthesizer.synthesize(plan, results)
            if self._synthesizer is not None
            else "\n".join(str(value.output.get("output", "")) for value in results.values())
        )
        if self._events is not None:
            from domain.events import ApplicationEvent, EventDurability, EventType

            await self._events.publish(ApplicationEvent(
                event_type=EventType.SYNTHESIS_COMPLETED,
                durability=EventDurability.DURABLE,
                session_id=plan.session_id or "",
                run_id=plan.run_id,
                payload={
                    "activity_id": f"synthesis:{plan_id}",
                    "activity_type": "synthesis",
                    "name": "synthesis",
                    "parent_activity_id": f"plan:{plan_id}",
                    "plan_id": plan_id,
                    "status": "completed",
                    "result": self._results[plan_id],
                },
            ))

    async def _publish_task(self, execution, event_type: str, payload: dict[str, object]) -> None:
        if self._events is None:
            return
        from domain.events import ApplicationEvent, EventDurability
        key = f"{execution.task.task_id}:{execution.execution_generation}"
        started = self._task_started.get(key)
        activity_id = f"task:{execution.task.task_id}:{execution.execution_generation}"
        details: dict[str, object] = {
            "activity_id": activity_id,
            "activity_type": "task",
            "name": execution.task.title,
            "parent_activity_id": f"plan:{execution.plan_id}",
            "plan_id": execution.plan_id,
            "task_id": execution.task.task_id,
            "title": execution.task.title,
            "execution_generation": execution.execution_generation,
            **payload,
        }
        if started:
            details["started_at"] = started[0].isoformat()
            if event_type in {"task.completed", "task.failed", "task.cancelled"}:
                details["duration_ms"] = int((perf_counter() - started[1]) * 1000)
                details["completed_at"] = datetime.now(timezone.utc).isoformat()
        await self._events.publish(ApplicationEvent(event_type=event_type, durability=EventDurability.DURABLE, session_id=execution.session_id, run_id=execution.run_id, payload=details))

    def synthesized_result(self, plan_id: str) -> str | None:
        return self._results.get(plan_id)


__all__ = ["WorkerSchedulerService"]
