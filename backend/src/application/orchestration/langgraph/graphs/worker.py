"""LangGraph adapter for the Worker application workflow."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from application.orchestration.protocols import WorkerWorkflowPort

from ..shared.utils import call
from .state import WorkerGraphState, json_value


def build_worker_graph(
    execute_tool: Callable[..., Any] | None = None,
    execute_worker: Callable[..., Any] | None = None,
    checkpointer: Any = None,
    *,
    workflow: WorkerWorkflowPort | None = None,
    worker: Any | None = None,
    execute: Callable[..., Any] | None = None,
    resume: Callable[..., Any] | None = None,
) -> Any:
    if workflow is None:
        raise ValueError("workflow is required; construct WorkerWorkflow in bootstrap")
    graph = StateGraph(WorkerGraphState)
    handlers = {
        "prepare_worker": workflow.prepare,
        "worker_agent_loop": workflow.run,
        "worker_loop": workflow.run,
        "resume_worker": workflow.resume_node,
        "approval_gate": workflow.approval_gate,
        "execute_tool_batch": workflow.execute_tool_batch,
        "finish_worker": workflow.finalize,
        "finalize_worker": workflow.finalize,
    }
    for name, handler in handlers.items():
        graph.add_node(name, handler)
    graph.add_edge(START, "prepare_worker")
    graph.add_conditional_edges(
        "prepare_worker",
        workflow.route,
        {"worker_agent_loop": "worker_agent_loop", "resume": "resume_worker"},
    )
    graph.add_edge("worker_agent_loop", "approval_gate")
    graph.add_edge("worker_loop", "approval_gate")
    graph.add_edge("resume_worker", "approval_gate")
    graph.add_conditional_edges(
        "approval_gate",
        workflow.route_after_gate,
        {"execute_tool_batch": "execute_tool_batch", "finish_worker": "finish_worker"},
    )
    graph.add_conditional_edges(
        "execute_tool_batch",
        workflow.route_after_tools,
        {
            "worker_agent_loop": "worker_agent_loop",
            "request_worker_approval": "request_worker_approval",
            "finish_worker": "finish_worker",
        },
    )
    async def request_worker_approval(state: WorkerGraphState) -> dict[str, Any]:
        return await _request_worker_approval(workflow, state)

    graph.add_node("request_worker_approval", request_worker_approval)
    graph.add_edge("request_worker_approval", "worker_agent_loop")
    graph.add_edge("finish_worker", END)
    graph.add_edge("finalize_worker", END)
    return graph.compile(checkpointer=checkpointer)


async def _request_worker_approval(
    workflow: WorkerWorkflowPort,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    """中断当前 Worker，并在 ResumeWorker 后执行原样冻结的操作。

    Args:
        workflow: 提供审批后工具执行用例的应用工作流。
        state: 包含待审批操作的可恢复 Worker 状态。

    Returns:
        恢复后写入图状态的工具结果。

    Raises:
        RuntimeError: 中断状态中没有有效待审批操作时抛出。
    """
    approval = state.get("pending_approval")
    if not isinstance(approval, Mapping) or not approval:
        raise RuntimeError("worker_approval_request_missing")
    decision = interrupt(dict(approval))
    return await workflow.complete_tool_approval(state, decision)


async def invoke_worker_graph(
    graph: Any,
    *,
    task_id: str,
    execution_generation: int,
    execution: Mapping[str, Any],
    decisions: dict[str, str] | None = None,
    resume_value: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not task_id:
        raise ValueError("task_id is required")
    encoded = json_value(execution)
    state: WorkerGraphState = {
        "execution": encoded if isinstance(encoded, dict) else {"value": encoded}
    }
    if decisions is not None:
        state["response"] = {"resume": True, "decisions": json_value(decisions)}
    thread_id = (
        encoded.get("worker_thread_id")
        if isinstance(encoded, Mapping)
        else None
    ) or f"task:{task_id}:exec:{execution_generation}"
    config = {"configurable": {"thread_id": str(thread_id)}}
    if resume_value is not None:
        current_execution = encoded if isinstance(encoded, Mapping) else {}
        checkpoint = await call(graph.aget_state, config=config)
        values = getattr(checkpoint, "values", {})
        saved_execution = (
            values.get("execution", {}) if isinstance(values, Mapping) else {}
        )
        merged_execution = (
            dict(saved_execution) if isinstance(saved_execution, Mapping) else {}
        )
        merged_execution.update(current_execution)
        for key in ("tool_results", "_worker_result_refs"):
            if key in saved_execution:
                merged_execution[key] = saved_execution[key]
        return await call(
            graph.ainvoke,
            Command(
                resume=dict(resume_value),
                update={"execution": merged_execution},
            ),
            config=config,
        )
    return await call(graph.ainvoke, state, config=config)


__all__ = ["build_worker_graph", "invoke_worker_graph"]
