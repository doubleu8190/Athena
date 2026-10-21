"""LangGraph adapter for the Worker application workflow."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from langgraph.graph import END, START, StateGraph

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
        {"worker_agent_loop": "worker_agent_loop", "finish_worker": "finish_worker"},
    )
    graph.add_edge("finish_worker", END)
    graph.add_edge("finalize_worker", END)
    return graph.compile(checkpointer=checkpointer)


async def invoke_worker_graph(
    graph: Any,
    *,
    task_id: str,
    execution_generation: int,
    execution: Mapping[str, Any],
    decisions: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not task_id:
        raise ValueError("task_id is required")
    encoded = json_value(execution)
    state: WorkerGraphState = {
        "execution": encoded if isinstance(encoded, dict) else {"value": encoded}
    }
    if decisions is not None:
        state["response"] = {"resume": True, "decisions": json_value(decisions)}
    return await call(
        graph.ainvoke,
        state,
        config={"configurable": {"thread_id": f"task:{task_id}:exec:{execution_generation}"}},
    )


__all__ = ["build_worker_graph", "invoke_worker_graph"]
