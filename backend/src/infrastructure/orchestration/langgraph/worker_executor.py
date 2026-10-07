"""Default Worker executor used by the orchestration scheduler."""

from __future__ import annotations

from typing import Any

from domain.orchestration import TaskExecution, TaskResult, TaskStatus, WorkerExecutor

from .worker import build_worker_graph, invoke_worker_graph


class LangGraphWorkerExecutor(WorkerExecutor):
    """Execute each persisted Task through an independent Worker graph."""

    def __init__(self, *, graph_factory: Any | None = None) -> None:
        """保存 Worker graph 工厂；未提供时使用默认 Worker graph。"""
        self._graph_factory = graph_factory
        self._graphs: dict[str, Any] = {}

    def _graph_for(self, execution: TaskExecution) -> Any:
        """按 Task 和执行代次创建或复用 Worker graph。"""
        key = f"{execution.task.task_id}:{execution.execution_generation}"
        if key not in self._graphs:
            self._graphs[key] = (
                self._graph_factory()
                if self._graph_factory is not None
                else build_worker_graph()
            )
        return self._graphs[key]

    async def execute(self, execution: TaskExecution) -> TaskResult:
        """执行 Worker graph，并将 JSON 结果转换为 TaskResult。"""
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
        """用审批决定恢复指定执行代次的 Worker graph。"""
        value = await invoke_worker_graph(
            self._graph_for(execution),
            task_id=execution.task.task_id,
            execution_generation=execution.execution_generation,
            execution=execution,
            decisions=decisions,
        )
        return self._result(execution, value)

    async def cancel(self, run_id: str) -> int:
        """取消当前进程中属于指定运行的 Worker；返回取消数量。"""
        return 0

    @staticmethod
    def _result(execution: TaskExecution, value: dict[str, Any]) -> TaskResult:
        """将 Worker graph 的最终状态转换为领域结果。"""
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
            error=payload.get("error"),
            approval_batch=payload.get("approval_batch"),
            retryable=bool(payload.get("retryable", False)),
        )


__all__ = ["LangGraphWorkerExecutor"]
