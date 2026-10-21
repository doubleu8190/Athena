"""校验并持久化 Worker Task 终态事件。"""

from __future__ import annotations

from domain.orchestration import (
    TaskEventReceipt,
    TaskEventRepository,
    WorkerTaskTerminalEvent,
)


class WorkerTaskEventHandler:
    """把 Worker 结果交给负责编排状态收敛的持久化边界。"""

    def __init__(self, repository: TaskEventRepository) -> None:
        """保存事件仓储。"""
        self._repository = repository

    async def apply_terminal_event(
        self, event: WorkerTaskTerminalEvent
    ) -> TaskEventReceipt:
        """接受一次 Worker 终态结果，并在需要时完成所属 Round。"""
        return await self._repository.apply_worker_terminal_event(event)


__all__ = ["WorkerTaskEventHandler"]
