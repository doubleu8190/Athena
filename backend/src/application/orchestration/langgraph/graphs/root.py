"""LangGraph adapter for the Root application workflow."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from langgraph.graph import END, START, StateGraph

from application.orchestration.protocols import (
    RootWorkflowDependencies,
    RootWorkflowPort,
)

from ..shared.utils import call
from .state import RootGraphState, json_value



async def _publish(
    dependencies: RootWorkflowDependencies,
    event_type: str,
    state: Mapping[str, Any],
    *,
    node_name: str,
    started_at: datetime | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    publisher = getattr(dependencies, "event_publisher", None)
    if publisher is None or not hasattr(publisher, "publish"):
        return
    request = state.get("request", {})
    try:
        from domain.events import ApplicationEvent, EventDurability

        await call(
            publisher.publish,
            ApplicationEvent(
                event_type=event_type,
                durability=EventDurability.DURABLE,
                session_id=str(request.get("session_id", "")),
                run_id=request.get("run_id"),
                message_id=request.get("message_id"),
                payload={
                    "activity_id": f"node:{request.get('run_id', '')}:{node_name}",
                    "activity_type": "node",
                    "parent_activity_id": f"run:{request.get('run_id', '')}",
                    "node": node_name,
                    "name": node_name,
                    "status": "running" if event_type == "node.started" else "failed" if event_type == "node.failed" else "completed",
                    **({"started_at": started_at.isoformat()} if started_at else {}),
                    **({"completed_at": datetime.now(timezone.utc).isoformat(), "duration_ms": duration_ms} if duration_ms is not None else {}),
                    **({"error": {"code": "node_failed", "message": error, "stage": node_name}} if error else {}),
                },
            ),
        )
    except Exception:
        return


def _node(
    name: str,
    handler: Callable[[RootGraphState], Any],
    dependencies: RootWorkflowDependencies,
):
    async def wrapped(state: RootGraphState) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc)
        started_clock = perf_counter()
        await _publish(dependencies, "node.started", state, node_name=name, started_at=started_at)
        try:
            value = await call(handler, state)
        except Exception as exc:
            await _publish(dependencies, "node.failed", state, node_name=name, started_at=started_at, duration_ms=int((perf_counter() - started_clock) * 1000), error=type(exc).__name__)
            return {
                "status": "failed",
                "error": {
                    "code": "node_failed",
                    "node": name,
                    "message": str(exc) or type(exc).__name__,
                },
                "next_route": "assemble_final_response",
            }
        await _publish(dependencies, "node.completed", state, node_name=name, started_at=started_at, duration_ms=int((perf_counter() - started_clock) * 1000))
        return value or {}

    wrapped.__name__ = name
    return wrapped


def _route_after_understanding(state: RootGraphState) -> str:
    understanding = state.get("understanding", {})
    task_spec = understanding.get("task_spec", {})
    return (
        "clarification_response"
        if (
            understanding.get("clarification_question")
            or understanding.get("requires_clarification")
            or task_spec.get("clarification_question")
            or task_spec.get("requires_clarification")
        )
        else "plan_context"
    )


def _route_after_agent(state: RootGraphState) -> str:
    route = state.get("next_route") or state.get("execution", {}).get("route")
    return (
        "materialize_execution_plan"
        if route in {"materialize_execution_plan", "plan_requested", "plan"}
        else "post_process_and_build_result"
    )


def _route_after_prepare(state: RootGraphState) -> str:
    if state.get("resume_value") and state.get("resume_value", {}).get("tool_call_id"):
        return "understand_task"
    return "project_plan_result" if state.get("resume_value") else "understand_task"


def _route_after_wait(state: RootGraphState) -> str:
    return "project_plan_result" if state.get("waiting") is False else END


def build_root_graph(
    *,
    workflow: RootWorkflowPort,
    dependencies: RootWorkflowDependencies ,
    checkpointer: Any = None,
) -> Any:
    """Compile a graph whose nodes delegate business work to an application port."""
    handlers = {
        "prepare_request": workflow.prepare_request,
        "understand_task": workflow.understand_task,
        "clarification_response": workflow.clarification_response,
        "plan_context": workflow.plan_context,
        "acquire_context": workflow.acquire_context,
        "prepare_harness_input": workflow.prepare_harness_input,
        "run_root_execution_loop": workflow.run_root_execution_loop,
        "materialize_execution_plan": workflow.materialize_execution_plan,
        "wait_for_plan_completion": workflow.wait_for_plan_completion,
        "project_plan_result": workflow.project_plan_result,
        "finalize_plan_stream": workflow.finalize_plan_stream,
        "post_process_and_build_result": workflow.post_process_and_build_result,
        "assemble_final_response": workflow.assemble_final_response,
    }
    graph = StateGraph(RootGraphState)
    for name, handler in handlers.items():
        graph.add_node(name, _node(name, handler, dependencies))
    graph.add_edge(START, "prepare_request")
    graph.add_conditional_edges(
        "prepare_request",
        _route_after_prepare,
        {
            "understand_task": "understand_task",
            "project_plan_result": "project_plan_result",
        },
    )
    graph.add_conditional_edges(
        "understand_task",
        _route_after_understanding,
        {
            "clarification_response": "clarification_response",
            "plan_context": "plan_context",
        },
    )
    graph.add_edge("clarification_response", "assemble_final_response")
    graph.add_edge("plan_context", "acquire_context")
    graph.add_edge("acquire_context", "prepare_harness_input")
    graph.add_edge("prepare_harness_input", "run_root_execution_loop")
    graph.add_conditional_edges(
        "run_root_execution_loop",
        _route_after_agent,
        {
            "post_process_and_build_result": "post_process_and_build_result",
            "materialize_execution_plan": "materialize_execution_plan",
        },
    )
    graph.add_conditional_edges(
        "materialize_execution_plan",
        lambda state: (
            "wait_for_plan_completion"
            if state.get("plan")
            and not state.get("error")
            and not state.get("response", {}).get("error")
            else "post_process_and_build_result"
        ),
        {
            "wait_for_plan_completion": "wait_for_plan_completion",
            "post_process_and_build_result": "post_process_and_build_result",
        },
    )
    graph.add_conditional_edges(
        "wait_for_plan_completion",
        _route_after_wait,
        {"project_plan_result": "project_plan_result", END: END},
    )
    graph.add_edge("project_plan_result", "finalize_plan_stream")
    graph.add_edge("finalize_plan_stream", "assemble_final_response")
    graph.add_edge("post_process_and_build_result", "assemble_final_response")
    graph.add_edge("assemble_final_response", END)
    return graph.compile(checkpointer=checkpointer)


async def invoke_root_graph(
    graph: Any,
    *,
    session_id: str,
    run_id: str,
    user_message: str = "",
    message_id: str = "",
    attachment_ids: list[str] | None = None,
    resume_value: dict[str, object] | None = None,
    stop_signal: Any = None,
) -> dict[str, Any]:
    if not run_id:
        raise ValueError("run_id is required")
    state: RootGraphState = {
        "request": {
            "session_id": session_id,
            "run_id": run_id,
            "message_id": message_id,
            "user_message": user_message,
            "attachment_ids": list(attachment_ids or []),
        }
    }
    if resume_value is not None:
        state["resume_value"] = json_value(resume_value)
    config = {"configurable": {"thread_id": f"run:{run_id}"}}
    if stop_signal is not None:
        config["configurable"]["stop_signal"] = stop_signal
    return await call(graph.ainvoke, state, config=config)


__all__ = ["build_root_graph", "invoke_root_graph"]
