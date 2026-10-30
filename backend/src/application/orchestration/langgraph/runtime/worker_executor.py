"""持久化 Worker 作业分发器使用的默认执行器。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from application.orchestration.protocols import WorkerExecutor
from domain.orchestration import TaskExecution, TaskResult, TaskStatus

from .protocols import CheckpointerProvider
from ..graphs.worker import build_worker_graph, invoke_worker_graph


class LangGraphWorkerExecutor(WorkerExecutor):
    """为每个持久化 Task 创建独立 Worker 图并负责调用。
    """

    def __init__(
        self,
        *,
        graph_factory: Any | None = None,
        workflow_factory: Callable[[], Any] | None = None,
        checkpointer: CheckpointerProvider | BaseCheckpointSaver[Any] | None = None,
    ) -> None:
        """保存 Worker 图工厂和线程锁，初始化图实例缓存。

        参数：
        - `graph_factory`（Any | None）：创建或编译工作流图的工厂函数；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `workflow_factory`（Callable[[], Any] | None）：创建应用工作流实例的工厂函数；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `checkpointer`（CheckpointerProvider | BaseCheckpointSaver[Any] | None）：保存和恢复图执行状态的检查点依赖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._graph_factory = graph_factory
        self._workflow_factory = workflow_factory
        self._checkpointer = checkpointer
        self._graphs: dict[str, Any] = {}

    def _graph_for(self, execution: TaskExecution) -> Any:
        """按 Task 和执行代次创建或复用 Worker 图。

        参数：
        - `execution`（TaskExecution）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。

        返回：
        - Any：与任务和执行代次绑定的已编译 Worker 图。

        异常：
        - RuntimeError：校验条件 `key not in self._graphs；not (self._graph_factory is not None)；not (self._workflow_factory is not None)` 成立时（异常说明：worker workflow factory is not configured）。
        - 未列出的依赖异常按当前实现传播。
        """
        key = f"{execution.task.task_id}:{execution.execution_generation}"
        if key not in self._graphs:
            if self._graph_factory is not None:
                self._graphs[key] = self._graph_factory()
            elif self._workflow_factory is not None:
                if isinstance(self._checkpointer, BaseCheckpointSaver):
                    saver = self._checkpointer
                elif self._checkpointer is not None:
                    saver = self._checkpointer.get_saver()
                else:
                    saver = None
                self._graphs[key] = build_worker_graph(
                    workflow=self._workflow_factory(),
                    checkpointer=saver,
                )
            else:
                raise RuntimeError("worker workflow factory is not configured")
        return self._graphs[key]

    async def execute(self, execution: TaskExecution) -> TaskResult:
        """调用指定 Task 代次的 Worker 图并转换最终结果。

        参数：
        - `execution`（TaskExecution）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。

        返回：
        - TaskResult：Worker 任务的最终状态、输出内容及失败信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        value = await invoke_worker_graph(
            self._graph_for(execution),
            task_id=execution.task.task_id,
            execution_generation=execution.execution_generation,
            execution=execution,
        )
        return self._result(execution, value)

    async def resume(
        self,
        execution: TaskExecution,
        decisions: dict[str, str],
    ) -> TaskResult:
        """根据审批决定恢复指定执行代次的 Worker 图。

        参数：
        - `execution`（TaskExecution）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。
        - `decisions`（dict[str, str]）：按工具调用标识记录的审批决定集合；约束：无额外运行时限制。

        返回：
        - TaskResult：Worker 任务的最终状态、输出内容及失败信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        value = await invoke_worker_graph(
            self._graph_for(execution),
            task_id=execution.task.task_id,
            execution_generation=execution.execution_generation,
            execution=execution,
            resume_value=decisions,
        )
        return self._result(execution, value)

    async def pending_interrupt(
        self, execution: TaskExecution
    ) -> dict[str, Any] | None:
        """读取尚未登记审批记录的持久化 Worker 中断。

        参数：
        - `execution`（TaskExecution）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。

        返回：
        - dict[str, Any] | None：尚未登记的 Worker 审批中断内容；没有待处理中断时返回 None。

        异常：
        - RuntimeError：校验条件 `len(interrupts) != 1` 成立时（异常说明：worker_graph_multiple_interrupts_not_supported）。
        - RuntimeError：校验条件 `not isinstance(value, dict)` 成立时（异常说明：worker_graph_interrupt_payload_invalid）。
        - 未列出的依赖异常按当前实现传播。
        """
        graph = self._graph_for(execution)
        thread_id = execution.worker_thread_id or (
            f"task:{execution.task.task_id}:exec:{execution.execution_generation}"
        )
        snapshot = await graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        interrupts = [
            pending
            for task in snapshot.tasks
            for pending in task.interrupts
        ]
        if not interrupts:
            return None
        if len(interrupts) != 1:
            raise RuntimeError("worker_graph_multiple_interrupts_not_supported")
        pending = interrupts[0]
        value = pending.value
        if not isinstance(value, dict):
            raise RuntimeError("worker_graph_interrupt_payload_invalid")
        recovered = dict(value)
        recovered["interrupt_id"] = str(pending.id)
        return recovered

    @staticmethod
    def _result(execution: TaskExecution, value: dict[str, Any]) -> TaskResult:
        """将 Worker graph 的最终状态转换为领域结果。

        参数：
        - `execution`（TaskExecution）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。
        - `value`（dict[str, Any]）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - TaskResult：Worker 任务的最终状态、输出内容及失败信息。

        异常：
        - RuntimeError：校验条件 `isinstance(interrupts, (list, tuple)) and interrupts；len(interrupts) != 1` 成立时（异常说明：worker_graph_multiple_interrupts_not_supported）。
        - RuntimeError：校验条件 `isinstance(interrupts, (list, tuple)) and interrupts；not isinstance(interrupt_value, dict)` 成立时（异常说明：worker_graph_interrupt_payload_invalid）。
        - 未列出的依赖异常按当前实现传播。
        """
        interrupts = value.get("__interrupt__", ())
        if isinstance(interrupts, (list, tuple)) and interrupts:
            if len(interrupts) != 1:
                raise RuntimeError("worker_graph_multiple_interrupts_not_supported")
            interrupt_value = interrupts[0].value
            if not isinstance(interrupt_value, dict):
                raise RuntimeError("worker_graph_interrupt_payload_invalid")
            interrupt_id = interrupts[0].id
            approval = dict(interrupt_value)
            approval["interrupt_id"] = str(interrupt_id or "")
            return TaskResult(
                execution.task.task_id,
                TaskStatus.WAITING_APPROVAL,
                approval_batch=approval,
            )
        payload = dict(value.get("result", {}))
        status = str(value.get("status", payload.get("status", "failed")))
        try:
            task_status = TaskStatus(status)
        except ValueError:
            task_status = TaskStatus.FAILED
        return TaskResult(
            execution.task.task_id,
            task_status,
            output=payload.get("output", {}),
            evidence=tuple(payload.get("evidence", ())),
            uncertainties=tuple(str(item) for item in payload.get("uncertainties", ())),
            error=payload.get("error"),
            approval_batch=payload.get("approval_batch"),
            retryable=bool(payload.get("retryable", False)),
        )


__all__ = ["LangGraphWorkerExecutor"]
