"""Worker Job 租约的 application 边界。"""

from __future__ import annotations

from domain.orchestration import (
    RunCancellationReceipt,
    WorkerJobLease,
    WorkerJobRepository,
)


class WorkerJobService:
    """使用持久仓储领取和续租 Worker 执行。"""

    def __init__(
        self, repository: WorkerJobRepository, *, lease_seconds: float = 30.0
    ) -> None:
        """保存 Job 仓储和租约时长。"""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._repository = repository
        self._lease_seconds = lease_seconds

    async def claim_next(self, worker_id: str) -> WorkerJobLease | None:
        """领取一个可执行 Job；没有容量或可用作业时返回 None。"""
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        return await self._repository.claim_next(
            worker_id, lease_seconds=self._lease_seconds
        )

    async def renew_lease(self, lease: WorkerJobLease) -> WorkerJobLease | None:
        """续租仍由当前 Worker 和 fencing token 持有的执行。"""
        return await self._repository.renew_lease(
            lease, lease_seconds=self._lease_seconds
        )

    async def acknowledge_cancelled(self, lease: WorkerJobLease) -> bool:
        """确认 Worker 已响应取消栅栏并释放执行租约。"""
        return await self._repository.acknowledge_cancelled(lease)

    async def reconcile_cancellations(self) -> list[RunCancellationReceipt]:
        """收敛已确认退出或租约过期隔离的 Run 取消。"""
        return await self._repository.reconcile_cancellations()


__all__ = ["WorkerJobService"]
