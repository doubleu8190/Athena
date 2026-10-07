"""Independent Worker Agent graph for one persisted Task."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from ._builder import GraphBuilder, call
from .state import WorkerGraphState, json_value


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(json_value(value))
    if hasattr(value, "__dict__"):
        return dict(json_value(vars(value)))
    return {"output": json_value(value)}


def build_worker_graph(
    execute_tool: Callable[..., Any] | None = None,
    execute_worker: Callable[..., Any] | None = None,
    checkpointer: Any = None,
    *,
    worker: Any | None = None,
    execute: Callable[..., Any] | None = None,
    resume: Callable[..., Any] | None = None,
) -> Any:
    """Build a Worker graph that can pause for approval and resume in place."""
    execute = execute or execute_worker

    async def prepare(state: WorkerGraphState) -> dict[str, Any]:
        execution = dict(state.get("execution", {}))
        return {"execution": json_value(execution), "input": json_value(state.get("input", execution.get("task", {})))}

    async def run(state: WorkerGraphState) -> dict[str, Any]:
        execution_fn = execute
        if execution_fn is None and worker is not None and hasattr(worker, "execute"):
            execution_fn = worker.execute
        if execution_fn is None:
            return {"result": {"status": "failed", "error": {"code": "worker_not_configured"}}}
        value = await call(execution_fn, execution=state.get("execution", {}), state=state)
        payload = _mapping(value)
        status = str(payload.get("status", "done"))
        return {"result": payload, "waiting": status == "waiting_approval"}

    async def approval_gate(state: WorkerGraphState) -> dict[str, Any]:
        result = state.get("result", {})
        if state.get("waiting"):
            return {"status": "waiting_approval"}
        return {"status": str(result.get("status", "done"))}

    async def execute_tool_batch(state: WorkerGraphState) -> dict[str, Any]:
        if execute_tool is None:
            return {"result": {"status": "failed", "error": {"code": "tool_executor_not_configured"}}, "waiting": False}
        result = state.get("result", {})
        calls = result.get("tool_calls", []) if isinstance(result, Mapping) else []
        tool_results = []
        for tool_call in calls:
            value = await call(execute_tool, tool_call=tool_call, execution=state.get("execution", {}), state=state)
            tool_results.append(_mapping(value))
        execution = dict(state.get("execution", {}))
        execution["tool_results"] = tool_results
        return {"execution": execution, "result": {}, "waiting": False}

    async def resume_node(state: WorkerGraphState) -> dict[str, Any]:
        resume_fn = resume
        if resume_fn is None and worker is not None and hasattr(worker, "resume"):
            resume_fn = worker.resume
        if resume_fn is None:
            return {"result": {"status": "failed", "error": {"code": "worker_resume_not_configured"}}, "waiting": False}
        decisions = state.get("response", {}).get("decisions", {})
        value = await call(resume_fn, execution=state.get("execution", {}), decisions=decisions, state=state)
        payload = _mapping(value)
        return {"result": payload, "waiting": str(payload.get("status", "done")) == "waiting_approval"}

    async def finalize(state: WorkerGraphState) -> dict[str, Any]:
        result = dict(state.get("result", {}))
        return {"result": result, "waiting": bool(state.get("waiting")), "status": "waiting_approval" if state.get("waiting") else str(result.get("status", "done"))}

    def route(state: WorkerGraphState) -> str:
        return "resume" if state.get("response", {}).get("resume") else "worker_agent_loop"

    def route_after_gate(state: WorkerGraphState) -> str:
        if state.get("waiting"):
            return "finish_worker"
        result = state.get("result", {})
        return "execute_tool_batch" if result.get("tool_calls") else "finish_worker"

    def route_after_tools(state: WorkerGraphState) -> str:
        return "finish_worker" if state.get("result", {}).get("error") else "worker_agent_loop"

    builder = GraphBuilder(WorkerGraphState)
    for name, handler in ((
        ("prepare_worker", prepare),
        ("worker_agent_loop", run),
        ("worker_loop", run),
        ("resume_worker", resume_node),
        ("approval_gate", approval_gate),
        ("execute_tool_batch", execute_tool_batch),
        ("finish_worker", finalize),
        ("finalize_worker", finalize),
    )):
        builder.add_node(name, handler)
    builder.add_edge(builder.start, "prepare_worker")
    builder.add_conditional("prepare_worker", route, {"worker_agent_loop": "worker_agent_loop", "resume": "resume_worker"})
    builder.add_edge("worker_agent_loop", "approval_gate")
    builder.add_edge("worker_loop", "approval_gate")
    builder.add_edge("resume_worker", "approval_gate")
    builder.add_conditional("approval_gate", route_after_gate, {"execute_tool_batch": "execute_tool_batch", "finish_worker": "finish_worker"})
    builder.add_conditional("execute_tool_batch", route_after_tools, {"worker_agent_loop": "worker_agent_loop", "finish_worker": "finish_worker"})
    builder.add_edge("finish_worker", builder.end)
    builder.add_edge("finalize_worker", builder.end)
    return builder.compile(checkpointer=checkpointer)


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
    state: WorkerGraphState = {"execution": dict(json_value(execution))}
    if decisions is not None:
        state["response"] = {"resume": True, "decisions": json_value(decisions)}
    return await call(
        graph.ainvoke,
        state,
        config={"configurable": {"thread_id": f"task:{task_id}:exec:{execution_generation}"}},
    )


__all__ = ["build_worker_graph", "invoke_worker_graph"]
