"""Root 应用工作流的 LangGraph 适配器。
"""

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
from application.orchestration.graph_state import (
    RootResumeIdentity,
    RootWaitIdentity,
)
from application.orchestration.protocols import RootWorkflowPort
from application.orchestration.root_decision import (
    ROOT_DECISION_ADAPTER,
    PlanDraft,
    PlanningFeedback,
)
from domain.runs import RoundCompletedResumeValue
from domain.leases import LeaseGrant
from shared.serialization import json_value
from application.contracts.runs import RoundCompletedResumeValueSchema
from application.runs.request_contracts import RootRequestState
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
    """发布带节点身份、执行耗时和结果摘要的 Root 工作流事件。

    参数：
    - `dependencies`（RootWorkflowDependencies）：构建或执行当前流程所需的协作依赖；约束：无额外运行时限制。
    - `event_type`（str）：要发布的事件类型名称；约束：无额外运行时限制。
    - `state`（Mapping[str, Any]）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。
    - `node_name`（str）：当前图节点的稳定名称；约束：无额外运行时限制。
    - `invocation_id`（str）：本次图或模型调用的关联标识；约束：无额外运行时限制。
    - `started_at`（datetime | None）：操作开始时间，用于记录运行时间信息；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
    - `duration_ms`（int | None）：本阶段测得的耗时毫秒数；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
    - `error`（str | None）：操作失败时捕获的异常或错误说明；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
    - `summary`（str | None）：面向用户或后续流程的简要结果说明；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

    返回：
    - None：操作完成后不提供返回数据。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    try:
        request = RootRequestState.model_validate(state.get("request", {}))
        from domain.events import ApplicationEvent, EventDurability

        await dependencies.event_publisher.publish(
            ApplicationEvent(
                event_type=event_type,
                durability=EventDurability.DURABLE,
                session_id=request.session_id,
                run_id=request.run_id or None,
                message_id=request.message_id or None,
                payload={
                    "activity_id": f"node:{request.run_id}:{invocation_id}",
                    "invocation_id": invocation_id,
                    "activity_type": "node",
                    "parent_activity_id": f"run:{request.run_id}",
                    "node": node_name,
                    "name": node_name,
                    "status": {
                        "node.started": "running",
                        "node.failed": "failed",
                    }.get(event_type, "completed"),
                    **({"started_at": started_at.isoformat()} if started_at else {}),
                    **(
                        {
                            "completed_at": datetime.now(timezone.utc).isoformat(),
                            "duration_ms": duration_ms,
                        }
                        if duration_ms is not None
                        else {}
                    ),
                    **(
                        {
                            "error": {
                                "code": "node_failed",
                                "message": error,
                                "stage": node_name,
                            }
                        }
                        if error
                        else {}
                    ),
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
    """包装 Root 工作流处理器，并统一记录节点开始、完成和失败事件。

    参数：
    - `name`（str）：目标名称，用于标识相应配置、工具或对象；约束：无额外运行时限制。
    - `handler`（Callable[[RootGraphState], Awaitable[dict[str, Any]]]）：处理单个工作流节点的异步函数；约束：无额外运行时限制。
    - `dependencies`（RootWorkflowDependencies）：构建或执行当前流程所需的协作依赖；约束：无额外运行时限制。

    返回：
    - Callable[..., Any]：供图构建器注册并处理 Root 节点状态的异步函数。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    async def wrapped(state: RootGraphState) -> dict[str, Any]:
        """执行计划完成等待节点，并转换等待状态或异常。

        参数：
        - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：包含计划等待完成状态或错误信息的 Root 图状态补丁。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
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
    """从节点返回值中提取适合展示的结果概述，不包含原文或模型推理。

    参数：
    - `node_name`（str）：当前图节点的稳定名称；约束：无额外运行时限制。
    - `value`（Any）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - str | None：适合写入事件的结果摘要；无可展示字段时返回 None。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    if not isinstance(value, Mapping):
        return None

    # 按节点挑选有限字段生成摘要，事件中不包含模型原文、用户正文或推理内容。
    if node_name == "prepare_request":
        request = value.get("request", {})
        attachments = (
            request.get("attachment_ids", []) if isinstance(request, Mapping) else []
        )
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
        action_labels = {
            "answer": "直接回答",
            "context": "继续查询资料",
            "plan": "制定执行计划",
        }
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
    """请求准备失败时终止；成功时先用原始问题预检索。

    参数：
    - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

    返回：
    - str：Root 图中表示下一节点名称的路由键。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return "assemble_final_response" if state.get("error") else "prefetch_context"


def _route_after_prefetch(state: RootGraphState) -> str:
    """预检索可恢复结果进入首次决策；不可恢复错误直接终止。

    参数：
    - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

    返回：
    - str：Root 图中表示下一节点名称的路由键。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return "assemble_final_response" if state.get("error") else "decide_root_action"


def _route_after_context(state: RootGraphState) -> str:
    """不可恢复的采集错误终止；其余结果进入专用再决策节点。

    参数：
    - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

    返回：
    - str：Root 图中表示下一节点名称的路由键。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return "assemble_final_response" if state.get("error") else "redecide_root_action"


def _route_after_decision(state: RootGraphState) -> str:
    """校验 Root 决策后，按回答、检索、计划或终止动作选择后续节点。

    参数：
    - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

    返回：
    - str：Root 图中表示下一节点名称的路由键。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    if state.get("error"):
        return "assemble_final_response"
    try:
        action = ROOT_DECISION_ADAPTER.validate_python(
            state.get("root_decision", {})
        ).action
    except ValidationError:
        return "assemble_final_response"
    return {
        "answer": "generate_answer",
        "context": "acquire_context",
        "plan": "generate_plan",
    }.get(str(action), "assemble_final_response")


def _route_after_redecision(state: RootGraphState) -> str:
    """根据重新生成的 Root 决策选择回答、计划或终止路径。

    参数：
    - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

    返回：
    - str：Root 图中表示下一节点名称的路由键。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    if state.get("error"):
        return "assemble_final_response"
    try:
        action = ROOT_DECISION_ADAPTER.validate_python(
            state.get("root_decision", {})
        ).action
    except ValidationError:
        return "assemble_final_response"
    return {"answer": "generate_answer", "plan": "generate_plan"}.get(
        action, "assemble_final_response"
    )


def _route_after_plan_generation(state: RootGraphState) -> str:
    """根据错误、规划反馈和计划草稿选择 Root 后续节点。

    参数：
    - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

    返回：
    - str：由错误、规划反馈和计划草稿决定的 Root 后续节点名称。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    if state.get("error"):
        return "assemble_final_response"
    if state.get("planning_feedback") is not None:
        PlanningFeedback.model_validate(state.get("planning_feedback"))
        return "decide_root_action"
    plan_draft = state.get("plan_draft")
    if plan_draft is None:
        return "assemble_final_response"
    PlanDraft.model_validate(plan_draft)
    return "materialize_execution_plan"


def _route_after_wait(state: RootGraphState) -> str:
    """根据等待状态选择继续等待或构建计划响应。

    参数：
    - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

    返回：
    - str：Root 图中表示下一节点名称的路由键。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return "wait_for_plan_completion" if state.get("waiting") else "build_plan_response"


def _wait_for_plan_completion_node(workflow: RootWorkflowPort):
    """暂停 Root，直至持久化的 Round 完成事件恢复其 checkpoint。

    参数：
    - `workflow`（RootWorkflowPort）：负责执行业务步骤的应用工作流；约束：无额外运行时限制。

    返回：
    - Callable[..., Any]：等待计划轮次完成并返回图状态补丁的节点函数。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """

    async def wait_for_plan_completion(state: RootGraphState) -> dict[str, Any]:
        """暂停 Root 图，等待计划轮次完成后恢复。

        参数：
        - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：包含等待状态及恢复身份的 Root 图状态补丁。

        异常：
        - RuntimeError：校验条件 `not isinstance(wait_context, Mapping)` 成立时（异常说明：root_round_wait_identity_missing）。
        - RuntimeError：校验条件 `捕获 ValidationError 后` 成立时（异常说明：root_round_wait_identity_missing）。
        - RuntimeError：校验条件 `wait_identity.run_id != request.run_id` 成立时（异常说明：root_round_wait_run_mismatch）。
        - RuntimeError：校验条件 `捕获 ValidationError 后` 成立时（异常说明：root_round_resume_value_invalid）。
        - RuntimeError：校验条件 `any((getattr(resume_identity, name) != getattr(wait_identity, name) for name in ('run_id', 'plan_id', 'round_id', 'plan_version')))` 成立时（异常说明：root_round_resume_identity_mismatch）。
        - 未列出的依赖异常按当前实现传播。
        """
        result = await workflow.wait_for_plan_completion(state)
        if not result.get("waiting"):
            return result
        wait_context = result.get("root_wait")
        request = RootRequestState.model_validate(state.get("request", {}))
        if not isinstance(wait_context, Mapping):
            raise RuntimeError("root_round_wait_identity_missing")
        try:
            wait_identity = RootWaitIdentity.model_validate(wait_context)
        except ValidationError as error:
            raise RuntimeError("root_round_wait_identity_missing") from error
        if wait_identity.run_id != request.run_id:
            raise RuntimeError("root_round_wait_run_mismatch")
        resume_value = interrupt(dict(wait_context))
        try:
            resume_identity = RootResumeIdentity.model_validate(resume_value)
        except ValidationError as error:
            raise RuntimeError("root_round_resume_value_invalid") from error
        if any(
            getattr(resume_identity, name) != getattr(wait_identity, name)
            for name in ("run_id", "plan_id", "round_id", "plan_version")
        ):
            raise RuntimeError("root_round_resume_identity_mismatch")
        await workflow.review_completed_round(
            {
                **state,
                "root_wait": wait_identity.model_dump(mode="json"),
                "resume_value": resume_identity.model_dump(
                    mode="json", exclude_none=True
                ),
            }
        )
        # 审查可能开启新一轮；返回等待状态，让图以新的 LangGraph 任务重新进入此节点并创建新中断。
        return await workflow.wait_for_plan_completion(state)

    wait_for_plan_completion.__name__ = "wait_for_plan_completion"
    return wait_for_plan_completion


def build_root_graph(
    *,
    workflow: RootWorkflowPort,
    dependencies: RootWorkflowDependencies,
    checkpointer: Any = None,
) -> Any:
    """注册 Root 工作流节点和路由规则，并编译可恢复的 LangGraph。

    参数：
    - `workflow`（RootWorkflowPort）：负责执行业务步骤的应用工作流；约束：无额外运行时限制。
    - `dependencies`（RootWorkflowDependencies）：构建或执行当前流程所需的协作依赖；约束：无额外运行时限制。
    - `checkpointer`（Any）：保存和恢复图执行状态的检查点依赖；约束：可省略，默认值为 `None`。

    返回：
    - Any：已编译且可使用检查点恢复的 Root 工作流图。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
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
    # 此节点负责 LangGraph 中断边界，不能由 _node 包装，否则会捕获中断并将其误报为应用错误。
    graph.add_node("wait_for_plan_completion", _wait_for_plan_completion_node(workflow))
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
    lease: LeaseGrant,
    user_message: str = "",
    message_id: str = "",
    run_sequence: int = 1,
    attachment_ids: list[str] | None = None,
    resume_value: RoundCompletedResumeValue | Mapping[str, Any] | None = None,
    stop_signal: asyncio.Event | None = None,
) -> Mapping | dict[str, str] | Any:
    """启动 Root 图，或用已验证的 Round 身份恢复持久化 checkpoint。

    参数：
    - `graph`（CompiledStateGraph[Any, Any, Any, Any]）：已编译或待执行的工作流图；约束：无额外运行时限制。
    - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
    - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：须通过方法内校验；失败条件见下方异常说明。
    - `lease`（LeaseGrant）：当前后台作业持有的租约授权及执行代次；约束：无额外运行时限制。
    - `user_message`（str）：本次 Run 对应的用户原始消息；约束：可省略，默认值为 `''`。
    - `message_id`（str）：关联消息的稳定标识；None 表示未绑定消息；约束：可省略，默认值为 `''`。
    - `run_sequence`（int）：会话内 Run 的单调递增序号；约束：可省略，默认值为 `1`。
    - `attachment_ids`（list[str] | None）：当前请求已授权使用的附件标识集合；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
    - `resume_value`（RoundCompletedResumeValue | Mapping[str, Any] | None）：图中断后用于恢复执行的持久化值；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`；须通过方法内校验；失败条件见下方异常说明。
    - `stop_signal`（asyncio.Event | None）：通知当前执行应停止的异步信号；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

    返回：
    - Mapping | dict[str, str] | Any：Root 图本次调用生成的状态映射。

    异常：
    - ValueError：校验条件 `not run_id` 成立时（异常说明：run_id is required）。
    - RuntimeError：校验条件 `resume_value is not None；isinstance(resume_value, Mapping)；捕获 ValidationError 后` 成立时（异常说明：root_resume_value_type_invalid）。
    - RuntimeError：校验条件 `resume_value is not None；not (isinstance(resume_value, Mapping))；not isinstance(resume_value, RoundCompletedResumeValue)` 成立时（异常说明：root_resume_value_type_invalid）。
    - RuntimeError：校验条件 `resume_value is not None；resume_value.type != 'round_completed'` 成立时（异常说明：root_resume_value_type_invalid）。
    - RuntimeError：校验条件 `isinstance(result, Mapping) and result.get('__interrupt__')；not checkpoint_id` 成立时（异常说明：root_wait_checkpoint_identity_missing）。
    - 未列出的依赖异常按当前实现传播。
    """
    if not run_id:
        raise ValueError("run_id is required")
    state: RootGraphState = {
        "execution_lease": json_value(lease),
        "request": {
            "session_id": session_id,
            "run_id": run_id,
            "message_id": message_id,
            "run_sequence": run_sequence,
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
    if resume_value is not None:
        if isinstance(resume_value, Mapping):
            try:
                resume_value = RoundCompletedResumeValueSchema.model_validate(
                    resume_value
                ).to_domain()
            except ValidationError as error:
                raise RuntimeError("root_resume_value_type_invalid") from error
        elif not isinstance(resume_value, RoundCompletedResumeValue):
            raise RuntimeError("root_resume_value_type_invalid")
        if resume_value.type != "round_completed":
            raise RuntimeError("root_resume_value_type_invalid")
        logger.info(
            f"Resuming root graph for run {run_id} from round {resume_value.round_id}"
        )
        result = await graph.ainvoke(
            Command(
                update={"execution_lease": json_value(lease)},
                resume=RoundCompletedResumeValueSchema.from_domain(
                    resume_value
                ).model_dump(mode="json")
            ),
            config=config,
        )
    else:
        logger.info(f"Invoking root graph for run {run_id}")
        result = await graph.ainvoke(state, config=config)
    # 处理 Root Agent 中断事件。
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
