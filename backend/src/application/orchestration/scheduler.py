"""Worker 并行调度、状态收敛和重试用例。"""

from __future__ import annotations

import asyncio

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
        max_retries: int = 1,
    ) -> None:
        self._repository = repository
        self._worker = worker
        self._synthesizer = synthesizer
        self._max_retries = max_retries
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._retry_counts: dict[str, int] = {}
        self._results: dict[str, str] = {}

    async def start_ready_tasks(self, plan_id: str) -> dict[str, object]:
        value = await self._repository.get(plan_id)
        if value is None:
            raise ValueError(f"plan not found: {plan_id}")
        plan, _ = value
        claimed = await self._repository.claim_ready_tasks(plan_id, plan.max_parallelism)
        for execution in claimed:
            key = f"{execution.task.task_id}:{execution.execution_generation}"
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
            return

        outcome = await self._repository.finish_task(
            execution.task.task_id,
            execution.execution_generation,
            status=result.status.value,
            output=dict(result.output) if result.status == TaskStatus.DONE else None,
            error=dict(result.error) if result.status != TaskStatus.DONE and result.error else None,
        )
        if not outcome.get("accepted"):
            return
        if result.status in {TaskStatus.FAILED, TaskStatus.TIMED_OUT} and result.retryable:
            retries = self._retry_counts.get(execution.task.task_id, 0)
            if retries < self._max_retries:
                self._retry_counts[execution.task.task_id] = retries + 1
                if await self._repository.retry_task(execution.task.task_id) is not None:
                    await self.start_ready_tasks(execution.plan_id)
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
        if self._synthesizer is None:
            return
        value = await self._repository.get(plan_id)
        if value is None:
            return
        plan, _tasks = value
        results = await self._repository.get_results(plan_id)
        self._results[plan_id] = await self._synthesizer.synthesize(plan, results)

    def synthesized_result(self, plan_id: str) -> str | None:
        return self._results.get(plan_id)


__all__ = ["WorkerSchedulerService"]
