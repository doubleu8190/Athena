"""启动阶段对持久化运行状态进行对账。"""

from __future__ import annotations

from typing import Any

from athena.contracts.ports import AgentStorePort
from athena.contracts.statuses import AgentRunStatus


class RecoveryReconciler:
    """收敛异常中断的运行状态。"""

    def __init__(
        self,
        store: AgentStorePort,
        *,
        orchestration: Any | None = None,
    ) -> None:
        """创建恢复协调器。

        参数:
            store: 提供命令和运行状态读写能力的存储端口。
            orchestration: 可选的中心编排任务账本。
        返回值:
            None。
        异常:
            不抛出业务异常。
        """
        self.store = store
        self._orchestration = orchestration

    async def reconcile(self) -> dict[str, int]:
        """执行一次启动恢复对账。

        参数:
            无。
        返回值:
            dict[str, int]: 包含被标记为已取消的运行数量。
        异常:
            存储读写失败时传播底层异常。
        """
        # 启动对账只收敛状态，不能把旧的 message.submit 重新放回队列；否则
        # Consumer 启动后会在用户没有点击“恢复”的情况下自动执行旧任务。
        mark_unknown = getattr(self.store, "mark_running_tool_calls_unknown", None)
        unknown_result = await mark_unknown() if mark_unknown is not None else 0
        unknown_tools = unknown_result if isinstance(unknown_result, int) else 0
        prepare = getattr(self.store, "prepare_runs_for_manual_recovery", None)
        recovery_result = await prepare() if prepare is not None else {}
        recovery_state = recovery_result if isinstance(recovery_result, dict) else {}
        reclaimed_tasks = 0
        if self._orchestration is not None:
            # 编排任务只重新置为可领取状态，不启动 PlanDispatcher；真正执行仍由
            # 用户恢复后重新进入 checkpoint 的 run_planned_orchestration 节点触发。
            reclaimed_tasks = await self._orchestration.requeue_stale_claimed_tasks()
        cancelled = 0
        for run in await self.store.list_recoverable_runs():
            if (
                run.cancel_requested
                and run.status != AgentRunStatus.CANCELLED
            ):
                await self.store.update_run_status(run.run_id, AgentRunStatus.CANCELLED)
                cancelled += 1
        return {
            "reclaimed_commands": 0,
            "reclaimed_tasks": reclaimed_tasks,
            "cancelled_runs": cancelled,
            "unknown_tool_calls": unknown_tools,
            "paused_runs": int(recovery_state.get("paused_runs", 0)),
            "held_commands": int(recovery_state.get("held_commands", 0)),
        }
