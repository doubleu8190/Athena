"""PlanDispatcher：并发领取并执行 V1 无依赖任务。"""

from __future__ import annotations

import asyncio
from typing import Callable, Awaitable

from athena.infrastructure.postgre.database import Database
from athena.runtime.orchestration.contracts import (
    ExecutionPlan,
    TaskSpec,
    TaskStatus,
    WorkerResult,
    WorkerResultStatus,
)
from athena.runtime.orchestration.worker import WorkerExecutor
from athena.runtime.orchestration.events import OrchestrationEventPublisher
from athena.contracts.events import EventType
from athena.utils.id_generation import generate_sub_run_id
from athena.utils.logging import get_logger

logger = get_logger(__name__)

TaskRunner = Callable[[TaskSpec, int], Awaitable[WorkerResult]]


class PlanDispatcher:
    """按并发上限调度同一计划内的无依赖任务。"""

    def __init__(
        self,
        db: Database,
        worker: WorkerExecutor,
        event_publisher: OrchestrationEventPublisher,
        lease_owner: str = "dispatcher",
    ) -> None:
        """绑定任务账本和 Worker 执行器。

        参数：
            db: 数据库门面，通过 ``db.orchestration`` 领取任务。
            worker: 执行单个任务的 Worker。
            event_publisher: 编排生命周期事件发布器。
            lease_owner: 当前 PlanDispatcher 实例标识。

        返回值：
            None。

        异常：
            不主动抛出业务异常。
        """
        self._db = db
        self._worker = worker
        self._event_publisher = event_publisher
        self._lease_owner = lease_owner
        self._plan_id_to_session_id: dict[str, str] = {}

    def register_plan_session(self, plan_id: str, session_id: str) -> None:
        """登记计划所属会话，用于任务事件发布。

        参数：
            plan_id: 执行计划标识。
            session_id: 所属会话。

        返回值：
            None。

        异常：
            不主动抛出业务异常。
        """
        self._plan_id_to_session_id[plan_id] = session_id

    async def execute_plan(
        self,
        plan: ExecutionPlan,
        session_id: str,
        stop_signal: asyncio.Event | None = None,
    ) -> dict[str, WorkerResult]:
        """并发执行计划任务并按 task_id 返回结果。

        参数：
            plan: 已持久化并校验的执行计划。
            session_id: 计划所属用户会话。
            stop_signal: 可选停止事件。

        返回值：
            dict[str, WorkerResult]: 每个 task_id 对应一个最终结果。

        异常：
            RuntimeError: 任务执行过程中出现未预期的异常。
        """
        semaphore = asyncio.Semaphore(plan.max_parallelism)

        async def _run_one(index: int, task: TaskSpec) -> WorkerResult:
            async with semaphore:
                session_id = self._plan_id_to_session_id.get(plan.plan_id, "")
                claimed = await self._db.orchestration.claim_next_task(
                    plan.plan_id, self._lease_owner
                )
                if claimed is None:
                    return self._unavailable(task)
                # 并发领取依赖数据库 CAS；候选索引只提供乐观提示。
                worker_run_id = (
                    f"{generate_sub_run_id(plan.root_run_id, index)}_{claimed.attempt}"
                )
                await self._db.orchestration.transition_task(
                    task.task_id,
                    TaskStatus.CLAIMED,
                    TaskStatus.RUNNING,
                    worker_run_id=worker_run_id,
                )
                await self._event_publisher.publish_task(
                    EventType.TASK_STARTED,
                    session_id=session_id,
                    plan_id=plan.plan_id,
                    task_id=task.task_id,
                    run_id=plan.root_run_id,
                    worker_run_id=worker_run_id,
                    attempt=claimed.attempt,
                    payload={"title": task.title},
                )
                result = await self._worker.execute(
                    task=task,
                    session_id=session_id,
                    parent_run_id=plan.root_run_id,
                    root_run_id=plan.root_run_id,
                    index=index,
                    attempt=claimed.attempt,
                    stop_signal=stop_signal,
                    run_id=worker_run_id,
                )
                await self._db.orchestration.save_task_result(result)
                await self._db.orchestration.transition_task(
                    task.task_id,
                    TaskStatus.RUNNING,
                    self._to_task_status(result.status),
                )
                await self._event_publisher.publish_task(
                    EventType.TASK_COMPLETED
                    if result.status == "completed"
                    else EventType.TASK_FAILED,
                    session_id=session_id,
                    plan_id=plan.plan_id,
                    task_id=task.task_id,
                    run_id=plan.root_run_id,
                    worker_run_id=result.run_id,
                    attempt=claimed.attempt,
                    payload={
                        "status": result.status,
                        "title": task.title,
                        "error": result.error_message,
                    },
                )
                return result

        results = await asyncio.gather(
            *[_run_one(index, task) for index, task in enumerate(plan.tasks)],
            return_exceptions=True,
        )
        output: dict[str, WorkerResult] = {}
        for task, result in zip(plan.tasks, results):
            if isinstance(result, WorkerResult):
                output[task.task_id] = result
            elif isinstance(result, BaseException):
                logger.error("dispatch_failed", task_id=task.task_id, error=str(result))
            else:
                logger.error(
                    "dispatch_unexpected", task_id=task.task_id, result=type(result).__name__
                )
        return output

    @staticmethod
    def _to_task_status(status: str) -> TaskStatus:
        return {
            "completed": TaskStatus.COMPLETED,
            "failed": TaskStatus.FAILED,
            "cancelled": TaskStatus.CANCELLED,
            "timed_out": TaskStatus.TIMED_OUT,
            "invalid_output": TaskStatus.INVALID_OUTPUT,
        }[status]

    @staticmethod
    def _unavailable(task: TaskSpec) -> WorkerResult:
        return WorkerResult(
            plan_id=task.plan_id,
            task_id=task.task_id,
            run_id=f"{task.plan_id}:unavailable",
            status=WorkerResultStatus.FAILED,
            error_code="task_not_available",
            error_message="task could not be claimed from the ledger",
        )
