"""LangGraph adapter for the Root application workflow."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from uuid import uuid4

from langgraph.graph.state import CompiledStateGraph, RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from application.orchestration.dependencies import RootWorkflowDependencies
from application.orchestration.protocols import RootWorkflowPort
from domain.runs import RoundCompletedResumeValue
from shared import get_logger

from .state import RootGraphState

logger = get_logger(__name__)

async def _publish(
    dependencies: RootWorkflowDependencies,
    event_type: str,
    state: Mapping[str, Any],
    *,
    node_name: str,
    invocation_id: str,
    started_at: datetime | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    summary: str | None = None,
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
                    "activity_id": f"node:{request.get('run_id', '')}:{invocation_id}",
                    "invocation_id": invocation_id,
                    "activity_type": "node",
                    "parent_activity_id": f"run:{request.get('run_id', '')}",
                    "node": node_name,
                    "name": node_name,
                    "status": {
                        "node.started": "running",
                        "node.failed": "failed",
                    }.get(event_type, "completed"),
                    **({"started_at": started_at.isoformat()} if started_at else {}),
                    **({"completed_at": datetime.now(timezone.utc).isoformat(), "duration_ms": duration_ms} if duration_ms is not None else {}),
                    **({"error": {"code": "node_failed", "message": error, "stage": node_name}} if error else {}),
                    **({"summary": summary[:500]} if summary else {}),
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
        invocation_id = uuid4().hex
        started_at = datetime.now(timezone.utc)
        started_clock = perf_counter()
        await _publish(
            dependencies,
            "node.started",
            state,
            node_name=name,
            invocation_id=invocation_id,
            started_at=started_at,
        )
        try:
            value = await handler(state)
        except Exception as exc:
            await _publish(
                dependencies,
                "node.failed",
                state,
                node_name=name,
                invocation_id=invocation_id,
                started_at=started_at,
                duration_ms=int((perf_counter() - started_clock) * 1000),
                error=type(exc).__name__,
            )
            return {
                "status": "failed",
                "error": {
                    "code": str(getattr(exc, "code", "node_failed")),
                    "node": name,
                    "message": str(exc) or type(exc).__name__,
                    **(
                        {"phase": str(getattr(exc, "phase"))}
                        if getattr(exc, "phase", None)
                        else {}
                    ),
                },
            }
        await _publish(
            dependencies,
            "node.completed",
            state,
            node_name=name,
            invocation_id=invocation_id,
            started_at=started_at,
            duration_ms=int((perf_counter() - started_clock) * 1000),
            summary=_node_result_summary(name, value),
        )
        return value or {}

    wrapped.__name__ = name
    return wrapped


def _node_result_summary(node_name: str, value: Any) -> str | None:
    """从节点返回值中提取适合展示的结果概述，不包含原文或模型推理。"""
    if not isinstance(value, Mapping):
        return None

    if node_name == "prepare_request":
        request = value.get("request", {})
        attachments = request.get("attachment_ids", []) if isinstance(request, Mapping) else []
        knowledge_bases = value.get("knowledge_bases", [])
        return (
            f"请求上下文已准备；附件 {len(attachments) if isinstance(attachments, list) else 0} 个，"
            f"可用知识库 {len(knowledge_bases) if isinstance(knowledge_bases, list) else 0} 个。"
        )

    if node_name in {"prefetch_context", "acquire_context"}:
        bundle = value.get("context_bundle", {})
        items = bundle.get("items", []) if isinstance(bundle, Mapping) else []
        outcomes = value.get("context_queries", [])
        count = len(items) if isinstance(items, list) else 0
        outcome_count = len(outcomes) if isinstance(outcomes, list) else 0
        return f"资料窗口包含 {count} 条资料；记录了 {outcome_count} 项查询结果。"

    if node_name in {"decide_root_action", "redecide_root_action"}:
        decision = value.get("root_decision", {})
        action = decision.get("action") if isinstance(decision, Mapping) else None
        action_labels = {"answer": "直接回答", "context": "继续查询资料", "plan": "制定执行计划"}
        if action in action_labels:
            return f"处理方式已确定：{action_labels[action]}。"
        return "本阶段结束，流程将按当前状态继续。"

    if node_name == "generate_plan":
        draft = value.get("plan_draft")
        if isinstance(draft, Mapping):
            tasks = draft.get("tasks", [])
            if isinstance(tasks, list):
                return f"已生成计划草稿，包含 {len(tasks)} 项任务。"
        feedback = value.get("planning_feedback")
        if isinstance(feedback, Mapping):
            missing = feedback.get("missing_information", [])
            count = len(missing) if isinstance(missing, list) else 0
            return f"计划需要补充信息，共 {count} 项。"
        return None

    if node_name == "materialize_execution_plan":
        plan = value.get("plan", {})
        tasks = plan.get("tasks", []) if isinstance(plan, Mapping) else []
        count = len(tasks) if isinstance(tasks, list) else 0
        return f"执行计划已提交，包含 {count} 项任务。"

    if node_name == "generate_answer":
        return "最终回答已生成，正文见下方消息。"

    if node_name == "assemble_final_response":
        status = value.get("status")
        return "请求处理完成。" if status == "completed" else "请求处理未完成。"

    return None


def _route_after_request(state: RootGraphState) -> str:
    """请求准备失败时终止；成功时先用原始问题预检索。"""
    return "assemble_final_response" if state.get("error") else "prefetch_context"


def _route_after_prefetch(state: RootGraphState) -> str:
    """预检索可恢复结果进入首次决策；不可恢复错误直接终止。

    参数：state 是预检索后的图状态。返回：下一个节点名。
    异常：不主动抛出业务异常。
    """
    return "assemble_final_response" if state.get("error") else "decide_root_action"


def _route_after_context(state: RootGraphState) -> str:
    """不可恢复的采集错误终止；其余结果进入专用再决策节点。"""
    return "assemble_final_response" if state.get("error") else "redecide_root_action"


def _route_after_decision(state: RootGraphState) -> str:
    """按合法 action 分流，不再根据 mode 或返回字典猜测意图。"""
    if state.get("error"):
        return "assemble_final_response"
    action = state.get("root_decision", {}).get("action")
    return {
        "answer": "generate_answer",
        "context": "acquire_context",
        "plan": "generate_plan",
    }.get(str(action), "assemble_final_response")


def _route_after_redecision(state: RootGraphState) -> str:
    if state.get("error"):
        return "assemble_final_response"
    return {
        "answer": "generate_answer",
        "plan": "generate_plan",
    }.get(str(state.get("root_decision", {}).get("action")), "assemble_final_response")


def _route_after_plan_generation(state: RootGraphState) -> str:
    if state.get("error"):
        return "assemble_final_response"
    if state.get("planning_feedback"):
        return "decide_root_action"
    return "materialize_execution_plan" if state.get("plan_draft") else "assemble_final_response"


def _route_after_wait(state: RootGraphState) -> str:
    return "wait_for_plan_completion" if state.get("waiting") else "build_plan_response"


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
        "prefetch_context": workflow.prefetch_context,
        "decide_root_action": workflow.decide_root_action,
        "redecide_root_action": workflow.redecide_root_action,
        "acquire_context": workflow.acquire_context,
        "generate_plan": workflow.generate_plan,
        "generate_answer": workflow.generate_answer,
        "materialize_execution_plan": workflow.materialize_execution_plan,
        "build_plan_response": workflow.build_plan_response,
        "assemble_final_response": workflow.assemble_final_response,
    }
    graph = StateGraph(RootGraphState)
    for name, handler in handlers.items():
        graph.add_node(name, _node(name, handler, dependencies))
    # This node owns LangGraph's interrupt boundary and must not be wrapped by
    # _node, which would catch and turn the interrupt into an application error.
    graph.add_node(
        "wait_for_plan_completion", _wait_for_plan_completion_node(workflow)
    )
    graph.add_edge(START, "prepare_request")
    graph.add_conditional_edges(
        "prepare_request",
        _route_after_request,
        {
            "prefetch_context": "prefetch_context",
            "assemble_final_response": "assemble_final_response",
        },
    )
    graph.add_conditional_edges(
        "prefetch_context",
        _route_after_prefetch,
        {
            "decide_root_action": "decide_root_action",
            "assemble_final_response": "assemble_final_response",
        },
    )
    graph.add_conditional_edges(
        "decide_root_action",
        _route_after_decision,
        {
            "assemble_final_response": "assemble_final_response",
            "generate_answer": "generate_answer",
            "acquire_context": "acquire_context",
            "generate_plan": "generate_plan",
        },
    )
    graph.add_conditional_edges(
        "acquire_context",
        _route_after_context,
        {
            "assemble_final_response": "assemble_final_response",
            "redecide_root_action": "redecide_root_action",
        },
    )
    graph.add_conditional_edges(
        "redecide_root_action",
        _route_after_redecision,
        {
            "assemble_final_response": "assemble_final_response",
            "generate_answer": "generate_answer",
            "generate_plan": "generate_plan",
        },
    )
    graph.add_conditional_edges(
        "generate_plan",
        _route_after_plan_generation,
        {
            "assemble_final_response": "assemble_final_response",
            "decide_root_action": "decide_root_action",
            "materialize_execution_plan": "materialize_execution_plan",
        },
    )
    graph.add_edge("generate_answer", "assemble_final_response")
    graph.add_conditional_edges(
        "materialize_execution_plan",
        lambda state: (
            "wait_for_plan_completion"
            if state.get("plan")
            and not state.get("error")
            and not state.get("response", {}).get("error")
            else "assemble_final_response"
        ),
        {
            "wait_for_plan_completion": "wait_for_plan_completion",
            "assemble_final_response": "assemble_final_response",
        },
    )
    graph.add_conditional_edges(
        "wait_for_plan_completion",
        _route_after_wait,
        {
            "wait_for_plan_completion": "wait_for_plan_completion",
            "build_plan_response": "build_plan_response",
        },
    )
    graph.add_edge("build_plan_response", "generate_answer")
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
    resume_value: RoundCompletedResumeValue | Mapping[str, Any] | None = None,
    stop_signal: asyncio.Event | None = None,
) -> Mapping | dict[str, str] | Any:
    """启动 Root 图，或用已验证的 Round 身份恢复持久化 checkpoint。

    参数：``resume_value`` 可为具体模型，或从命令存储读出的同构 JSON 对象。
    返回：图执行状态；等待 Round 时包含 checkpoint 与 interrupt 信息。
    异常：Run 身份缺失、恢复内容不符合当前契约或图执行失败时抛出。
    """
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
    config: RunnableConfig = {
        "configurable": {"thread_id": f"run:{run_id}"},
        "recursion_limit": 128,
    }

    if stop_signal is not None:
        config["configurable"]["stop_signal"] = stop_signal
    # 恢复中断流程
    if resume_value is not None:
        if isinstance(resume_value, Mapping):
            try:
                resume_value = RoundCompletedResumeValue.model_validate(resume_value)
            except ValidationError as error:
                raise RuntimeError("root_resume_value_type_invalid") from error
        elif not isinstance(resume_value, RoundCompletedResumeValue):
            raise RuntimeError("root_resume_value_type_invalid")
        if resume_value.type != "round_completed":
            raise RuntimeError("root_resume_value_type_invalid")
        logger.info(f"Resuming root graph for run {run_id} from round {resume_value.round_id}")
        result = await graph.ainvoke(
            Command(resume=dict(resume_value.model_dump(mode="json"))),
            config=config,
        )
    else:
        # 任务启动流程
        logger.info(f"Invoking root graph for run {run_id}")
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
