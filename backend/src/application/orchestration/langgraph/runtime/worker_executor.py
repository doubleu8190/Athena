"""Default Worker executor used by the durable Job dispatcher."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from application.orchestration.protocols import WorkerExecutor
from domain.orchestration import TaskExecution, TaskResult, TaskStatus

from .protocols import CheckpointerProvider
from ..graphs.worker import build_worker_graph, invoke_worker_graph


class LangGraphWorkerExecutor(WorkerExecutor):
    """Execute each persisted Task through an independent Worker graph."""

    def __init__(
        self,
        *,
        graph_factory: Any | None = None,
        workflow_factory: Callable[[], Any] | None = None,
        checkpointer: CheckpointerProvider | BaseCheckpointSaver[Any] | None = None,
    ) -> None:
        """保存 Worker graph 工厂；未提供时使用默认 Worker graph。"""
        self._graph_factory = graph_factory
        self._workflow_factory = workflow_factory
        self._checkpointer = checkpointer
        self._graphs: dict[str, Any] = {}

    def _graph_for(self, execution: TaskExecution) -> Any:
        """按 Task 和执行代次创建或复用 Worker graph。"""
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
            resume_value=decisions,
        )
        return self._result(execution, value)

    async def pending_interrupt(
        self, execution: TaskExecution
    ) -> dict[str, Any] | None:
        """Recover an interrupt persisted before its Approval row was created."""
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
        """将 Worker graph 的最终状态转换为领域结果。"""
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
