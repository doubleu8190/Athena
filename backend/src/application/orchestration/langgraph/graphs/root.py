"""LangGraph adapter for the Root application workflow."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Mapping

from langgraph.graph.state import CompiledStateGraph
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from application.orchestration.dependencies import RootWorkflowDependencies
from application.orchestration.protocols import RootWorkflowPort
from domain.runs import RoundCompletedResumeValue

from .state import RootGraphState



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
    request = state.get("request", {})
    try:
        from domain.events import ApplicationEvent, EventDurability

        await dependencies.event_publisher.publish(
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
    handler: Callable[[RootGraphState], Awaitable[dict[str, Any]]],
    dependencies: RootWorkflowDependencies,
):
    async def wrapped(state: RootGraphState) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc)
        started_clock = perf_counter()
        await _publish(dependencies, "node.started", state, node_name=name, started_at=started_at)
        try:
            value = await handler(state)
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


def _route_after_wait(state: RootGraphState) -> str:
    return "wait_for_plan_completion" if state.get("waiting") else "project_plan_result"


def _wait_for_plan_completion_node(workflow: RootWorkflowPort):
    """Suspend Root until a durable Round completion resumes its checkpoint."""

    async def wait_for_plan_completion(state: RootGraphState) -> dict[str, Any]:
        result = await workflow.wait_for_plan_completion(state)
        if not result.get("waiting"):
            return result
        wait_context = result.get("root_wait")
        request = state.get("request", {})
        if (
            not isinstance(wait_context, Mapping)
            or any(
                not wait_context.get(name)
                for name in ("run_id", "plan_id", "round_id", "plan_version")
            )
        ):
            raise RuntimeError("root_round_wait_identity_missing")
        if wait_context.get("run_id") != request.get("run_id"):
            raise RuntimeError("root_round_wait_run_mismatch")
        resume_value = interrupt(dict(wait_context))
        if not isinstance(resume_value, Mapping):
            raise RuntimeError("root_round_resume_value_invalid")
        if resume_value.get("type") != "round_completed":
            raise RuntimeError("root_round_resume_type_invalid")
        if any(
            resume_value.get(name) != wait_context.get(name)
            for name in ("run_id", "plan_id", "round_id", "plan_version")
        ):
            raise RuntimeError("root_round_resume_identity_mismatch")
        await workflow.review_completed_round(
            {
                **state,
                "root_wait": dict(wait_context),
                "resume_value": dict(resume_value),
            }
        )
        # A review can start another Round. Return its wait state so the graph
        # re-enters this node as a new LangGraph task with a fresh interrupt.
        return await workflow.wait_for_plan_completion(state)

    wait_for_plan_completion.__name__ = "wait_for_plan_completion"
    return wait_for_plan_completion


def build_root_graph(
    *,
    workflow: RootWorkflowPort,
    dependencies: RootWorkflowDependencies,
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
        if name == "wait_for_plan_completion":
            graph.add_node(name, _wait_for_plan_completion_node(workflow))
        else:
            graph.add_node(name, _node(name, handler, dependencies))
    graph.add_edge(START, "prepare_request")
    graph.add_edge("prepare_request", "understand_task")
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
        {
            "wait_for_plan_completion": "wait_for_plan_completion",
            "project_plan_result": "project_plan_result",
        },
    )
    graph.add_edge("project_plan_result", "finalize_plan_stream")
    graph.add_edge("finalize_plan_stream", "assemble_final_response")
    graph.add_edge("post_process_and_build_result", "assemble_final_response")
    graph.add_edge("assemble_final_response", END)
    return graph.compile(checkpointer=checkpointer)


async def invoke_root_graph(
    graph: CompiledStateGraph[Any, Any, Any, Any],
    *,
    session_id: str,
    run_id: str,
    user_message: str = "",
    message_id: str = "",
    attachment_ids: list[str] | None = None,
    resume_value: RoundCompletedResumeValue | None = None,
    stop_signal: asyncio.Event| None = None,
) -> Mapping | dict[str, str] | Any:
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
    config = {"configurable": {"thread_id": f"run:{run_id}"}}

    if stop_signal is not None:
        config["configurable"]["stop_signal"] = stop_signal
    # 恢复中断流程
    if resume_value is not None:
        if resume_value.type != "round_completed":
            raise RuntimeError("root_resume_value_type_invalid")
        result = await graph.ainvoke(
            Command(resume=dict(resume_value.model_dump(mode="json"))),
            config=config,
        )
    else:
        # 任务启动流程
        result = await graph.ainvoke(state, config=config)
    # 处理root agent interrupt事件
    if isinstance(result, Mapping) and result.get("__interrupt__"):
        snapshot = await graph.aget_state(config)
        snapshot_config = snapshot.config
        configurable = (
            snapshot_config.get("configurable", {})
            if isinstance(snapshot_config, Mapping)
            else {}
        )
        checkpoint_id = (
            configurable.get("checkpoint_id")
            if isinstance(configurable, Mapping)
            else None
        )
        if not checkpoint_id:
            raise RuntimeError("root_wait_checkpoint_identity_missing")
        result = {**result, "__root_checkpoint_id__": str(checkpoint_id)}
    return result


__all__ = ["build_root_graph", "invoke_root_graph"]
