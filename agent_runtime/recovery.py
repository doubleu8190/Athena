"""启动阶段对持久化运行状态进行对账。"""

from __future__ import annotations

from athena.contracts.ports import AgentStorePort
from athena.contracts.statuses import AgentRunStatus


class RecoveryReconciler:
    """收敛异常中断的运行状态。"""

    def __init__(self, store: AgentStorePort) -> None:
        """创建恢复协调器。

        参数:
            store (AgentStorePort): 提供命令和运行状态读写能力的存储端口。
        返回值:
            None。
        异常:
            不抛出业务异常。
        """
        self.store = store

    async def reconcile(self) -> dict[str, int]:
        """执行一次启动恢复对账。

        参数:
            无。
        返回值:
            dict[str, int]: 包含被标记为已取消的运行数量。
        异常:
            存储读写失败时传播底层异常。
        """
        cancelled = 0
        for run in await self.store.list_recoverable_runs():
            if (
                run.cancel_requested
                and run.status != AgentRunStatus.CANCELLED
            ):
                await self.store.update_run_status(run.run_id, AgentRunStatus.CANCELLED)
                cancelled += 1
        return {"cancelled_runs": cancelled}
