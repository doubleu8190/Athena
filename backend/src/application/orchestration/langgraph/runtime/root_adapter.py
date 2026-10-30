"""由 LangGraph 实现的 Root 图应用端口。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from application.orchestration.protocols import (
    GraphThreadLock,
    RootGraphPort,
    RootWorkflowPort,
)
from application.orchestration.dependencies import RootWorkflowDependencies
from domain.leases import LeaseCenterPort, LeaseGrant
from domain.runs import RoundCompletedResumeValue

from .protocols import CheckpointerProvider
from ..graphs.root import build_root_graph, invoke_root_graph


class LangGraphRootGraph(RootGraphPort):
    """由已编译 Root 图支持的 RootGraphPort 实现。
    """

    def __init__(
        self,
        *,
        workflow: RootWorkflowPort,
        dependencies: RootWorkflowDependencies,
        checkpointer: CheckpointerProvider,
        thread_lock: GraphThreadLock,
        lease_center: LeaseCenterPort,
    ) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `workflow`（RootWorkflowPort）：负责执行业务步骤的应用工作流；约束：无额外运行时限制。
        - `dependencies`（RootWorkflowDependencies）：构建或执行当前流程所需的协作依赖；约束：无额外运行时限制。
        - `checkpointer`（CheckpointerProvider）：保存和恢复图执行状态的检查点依赖；约束：无额外运行时限制。
        - `thread_lock`（GraphThreadLock）：按检查点线程串行化图调用的锁依赖；约束：无额外运行时限制。
        - `lease_center`（LeaseCenterPort）：协调后台作业租约的可选应用端口；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._workflow = workflow
        self._dependencies = dependencies
        self._checkpointer = checkpointer
        self._thread_lock = thread_lock
        self._leases = lease_center
        self._graph: CompiledStateGraph[Any, Any, Any, Any] | None = None

    def _compiled_graph(self) -> CompiledStateGraph[Any, Any, Any, Any]:
        """在检查点保存器启动后延迟编译并缓存 Root 图。

        参数：
        - 无参数。

        返回：
        - CompiledStateGraph[Any, Any, Any, Any]：记录当前流程数据和阶段信息的 `CompiledStateGraph[Any, Any, Any, Any]` 状态对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        if self._graph is None:
            self._graph = build_root_graph(
                workflow=self._workflow,
                dependencies=self._dependencies,
                checkpointer=self._checkpointer.get_saver(),
            )
        return self._graph

    async def invoke(
        self,
        *,
        session_id: str,
        run_id: str,
        lease: LeaseGrant,
        user_message: str,
        message_id: str = "",
        run_sequence: int = 1,
        attachment_ids: list[str] | None = None,
        resume_value: RoundCompletedResumeValue | None = None,
        stop_signal: asyncio.Event | None = None,
    ) -> object:
        """按 Run 身份启动 Root 图，或恢复其持久化检查点。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
        - `lease`（LeaseGrant）：当前后台作业持有的租约授权及执行代次；约束：无额外运行时限制。
        - `user_message`（str）：本次 Run 对应的用户原始消息；约束：无额外运行时限制。
        - `message_id`（str）：关联消息的稳定标识；None 表示未绑定消息；约束：可省略，默认值为 `''`。
        - `run_sequence`（int）：会话内 Run 的单调递增序号；约束：可省略，默认值为 `1`。
        - `attachment_ids`（list[str] | None）：当前请求已授权使用的附件标识集合；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `resume_value`（RoundCompletedResumeValue | None）：图中断后用于恢复执行的持久化值；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `stop_signal`（asyncio.Event | None）：通知当前执行应停止的异步信号；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - object：Root 图执行后产生的状态映射；恢复调用复用对应检查点。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        async def invoke() -> object:
            """按 Run 身份启动 Root 图，或恢复其持久化检查点。

            参数：
            - 无参数。

            返回：
            - object：Root 图执行后产生的状态映射；恢复调用复用对应检查点。

            异常：
            - 无预期业务异常；依赖调用产生的异常按当前实现传播。
            """
            return await invoke_root_graph(
                self._compiled_graph(),
                session_id=session_id,
                run_id=run_id,
                lease=lease,
                user_message=user_message,
                message_id=message_id,
                run_sequence=run_sequence,
                attachment_ids=attachment_ids,
                resume_value=resume_value,
                stop_signal=stop_signal,
            )

        async with self._thread_lock.hold(f"run:{run_id}"):
            await self._leases.assert_current(lease)
            return await invoke()

    async def has_pending_round_wait(
        self,
        *,
        run_id: str,
        plan_id: str,
        round_id: str,
        plan_version: int,
        interrupt_id: str,
        checkpoint_id: str,
    ) -> bool:
        """检查当前 checkpoint 是否正等待指定版本和轮次的中断。

        参数：
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
        - `plan_id`（str）：目标计划的稳定标识；约束：无额外运行时限制。
        - `round_id`（str）：目标执行轮次的稳定标识；约束：无额外运行时限制。
        - `plan_version`（int）：目标计划版本号，用于固定读取或恢复的版本；约束：无额外运行时限制。
        - `interrupt_id`（str）：目标图中断的稳定标识；约束：无额外运行时限制。
        - `checkpoint_id`（str）：目标 LangGraph 检查点的稳定标识；约束：无额外运行时限制。

        返回：
        - bool：操作是否成功或条件是否成立。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        pending = await self.find_pending_round_wait(
            run_id=run_id,
            plan_id=plan_id,
            round_id=round_id,
            plan_version=plan_version,
        )
        return pending == (interrupt_id, checkpoint_id)

    async def find_pending_round_wait(
        self,
        *,
        run_id: str,
        plan_id: str,
        round_id: str,
        plan_version: int,
    ) -> tuple[str, str] | None:
        """从持久 checkpoint 找回尚未登记到 Run 行的 Round 中断。

        参数：
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
        - `plan_id`（str）：目标计划的稳定标识；约束：无额外运行时限制。
        - `round_id`（str）：目标执行轮次的稳定标识；约束：无额外运行时限制。
        - `plan_version`（int）：目标计划版本号，用于固定读取或恢复的版本；约束：无额外运行时限制。

        返回：
        - tuple[str, str] | None：检查点标识和中断标识；找不到符合条件的等待时返回 None。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        snapshot = await self._compiled_graph().aget_state(
            {"configurable": {"thread_id": f"run:{run_id}"}}
        )
        snapshot_config = snapshot.config
        configurable = (
            snapshot_config.get("configurable", {})
            if isinstance(snapshot_config, Mapping)
            else {}
        )
        if not isinstance(configurable, Mapping):
            return None
        checkpoint_id = configurable.get("checkpoint_id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            return None
        for task in snapshot.tasks:
            for pending in task.interrupts:
                value = pending.value
                if (
                    isinstance(value, Mapping)
                    and value.get("type") == "round_wait"
                    and value.get("run_id") == run_id
                    and value.get("plan_id") == plan_id
                    and value.get("round_id") == round_id
                    and value.get("plan_version") == plan_version
                ):
                    interrupt_id = pending.id
                    if isinstance(interrupt_id, str) and interrupt_id:
                        return interrupt_id, checkpoint_id
        return None


__all__ = ["LangGraphRootGraph"]
