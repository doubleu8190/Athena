"""保存 Root 提交的一轮 Worker Plan 的应用用例。"""

from __future__ import annotations

from domain.orchestration.ports import WorkerOrchestrationRepository
from domain.orchestration.worker_entities import (
    WorkerPlanReceipt,
    WorkerPlanSubmission,
)


class WorkerPlanSubmissionService:
    """把构造完成的 Plan 提交给 Orchestrator 持久化。"""

    def __init__(self, repository: WorkerOrchestrationRepository) -> None:
        """初始化 Plan 提交用例。

        参数：
        - `repository`（WorkerOrchestrationRepository）：负责原子验证和保存的持久化端口。

        返回：无。

        异常：此构造函数不主动抛出业务异常。
        """
        self._repository = repository

    async def submit(
        self, submission: WorkerPlanSubmission
    ) -> WorkerPlanReceipt:
        """提交一轮 Plan，并返回稳定的持久化回执。

        参数：
        - `submission`（WorkerPlanSubmission）：完整、无依赖且带幂等键的一轮任务定义。

        返回：已接受的 `WorkerPlanReceipt`；同请求重放会标记 `replayed`。

        异常：仓储的 `WorkerPlanRejected`、`WorkerIdempotencyConflict` 等错误原样传播。
        """
        return await self._repository.submit_plan(submission)


__all__ = ["WorkerPlanSubmissionService"]
