"""Root 应用工作流的 LangGraph 适配器。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from langgraph.graph.state import CompiledStateGraph, RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.errors import GraphInterrupt
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
from domain.runs import RootPlanCheckpointRecovery, RootPlanCompletedResumeValue
from domain.leases import LeaseGrant
from shared.serialization import json_value
from application.runs.request_contracts import RootRequestState
from application.observability import (
    RootNodeSpanAttributes,
    bind_span,
    reset_span,
    run_span_id,
)
from shared import get_logger

from .state import RootGraphState

logger = get_logger(__name__)


def _node(
    name: str,
    handler: Callable[[RootGraphState], Awaitable[dict[str, Any]]],
    dependencies: RootWorkflowDependencies,
):
    """包装 Root 工作流处理器并记录节点 Trace span。

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
        request = RootRequestState.model_validate(state.get("request", {}))
        span = None
        token = None
        if dependencies.observability is not None and request.run_id:
            span = await dependencies.observability.start_span(
                session_id=request.session_id,
                run_id=request.run_id,
                name=name,
                attributes=RootNodeSpanAttributes(node_name=name),
                parent_span_id=run_span_id(request.run_id),
            )
            token = bind_span(span.span_id)
        try:
            value = await handler(state)
        except GraphInterrupt:
            if dependencies.observability is not None and span is not None:
                await dependencies.observability.finish_span(
                    span.span_id, status="suspended"
                )
            raise
        except Exception as exc:
            if dependencies.observability is not None and span is not None:
                await dependencies.observability.finish_span(
                    span.span_id,
                    status="failed",
                    error={
                        "code": str(getattr(exc, "code", "node_failed")),
                        "message": type(exc).__name__,
                    },
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
        finally:
            if token is not None:
                reset_span(token)
        if dependencies.observability is not None and span is not None:
            await dependencies.observability.finish_span(span.span_id)
        return value or {}

    wrapped.__name__ = name
    return wrapped


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


def _route_after_decision(state: RootGraphState) -> str:
    """校验统一 Root 决策后，按回答、计划或终止动作选择后续节点。

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
        "plan": "generate_plan",
    }.get(str(action), "assemble_final_response")


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
    """根据等待状态选择继续等待或进入统一 Root 决策。

    参数：
    - `state`（RootGraphState）：当前可恢复的工作流状态，包含本阶段所需数据；约束：无额外运行时限制。

    返回：
    - str：Root 图中表示下一节点名称的路由键。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return "wait_for_plan_completion" if state.get("waiting") else "decide_root_action"


def _wait_for_plan_completion_node(workflow: RootWorkflowPort):
    """暂停 Root，直至当前 Plan 的所有任务完成。

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
        - RuntimeError：等待身份缺失或格式错误时，说明为 `root_plan_wait_identity_missing`。
        - RuntimeError：等待身份与当前 Run 不一致时，说明为 `root_plan_wait_run_mismatch`。
        - RuntimeError：恢复值缺失、格式错误或身份不一致时，说明为对应的 `root_plan_resume_*` 错误。
        - RuntimeError：校验条件 `恢复值与等待中的 Run、Plan、序号或代次不一致` 成立时（异常说明：root_plan_resume_identity_mismatch）。
        - 未列出的依赖异常按当前实现传播。
        """
        # 恢复命令已经明确确认当前 Plan 完成，直接进入消费节点；否则图从节点起点重放时会先读到完成事实，绕过 interrupt 后的消费逻辑。
        if state.get("resume_value"):
            return await workflow.consume_completed_plan(state)
        result = await workflow.wait_for_plan_completion(state)
        if not result.get("waiting"):
            return result
        wait_context = result.get("root_wait")
        request = RootRequestState.model_validate(state.get("request", {}))
        if not isinstance(wait_context, Mapping):
            raise RuntimeError("root_plan_wait_identity_missing")
        try:
            wait_identity = RootWaitIdentity.model_validate(wait_context)
        except ValidationError as error:
            raise RuntimeError("root_plan_wait_identity_missing") from error
        if wait_identity.run_id != request.run_id:
            raise RuntimeError("root_plan_wait_run_mismatch")
        resume_value = interrupt(dict(wait_context))
        try:
            resume_identity = RootResumeIdentity.model_validate(resume_value)
        except ValidationError as error:
            raise RuntimeError("root_plan_resume_value_invalid") from error
        if any(
            getattr(resume_identity, name) != getattr(wait_identity, name)
            for name in (
                "run_id",
                "plan_id",
                "plan_sequence",
                "root_wait_generation",
            )
        ):
            raise RuntimeError("root_plan_resume_identity_mismatch")
        return await workflow.consume_completed_plan(
            {
                **state,
                "root_wait": wait_identity.model_dump(mode="json"),
                "resume_value": resume_identity.model_dump(
                    mode="json", exclude_none=True
                ),
            }
        )

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
        "generate_plan": workflow.generate_plan,
        "generate_answer": workflow.generate_answer,
        "materialize_execution_plan": workflow.materialize_execution_plan,
        "build_plan_response": workflow.build_plan_response,
        "assemble_final_response": workflow.assemble_final_response,
    }
    graph = StateGraph(RootGraphState)
    for name, handler in handlers.items():
        graph.add_node(name, _node(name, handler, dependencies))
    graph.add_node(
        "wait_for_plan_completion",
        _node(
            "wait_for_plan_completion",
            _wait_for_plan_completion_node(workflow),
            dependencies,
        ),
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
            "decide_root_action": "decide_root_action",
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
    resume_value: RootPlanCompletedResumeValue | RootPlanCheckpointRecovery | Mapping[str, Any] | None = None,
    stop_signal: asyncio.Event | None = None,
) -> Mapping | dict[str, str] | Any:
    """启动 Root 图，或用已验证的 Plan 身份恢复持久化 checkpoint。

    参数：
    - `graph`（CompiledStateGraph[Any, Any, Any, Any]）：已编译或待执行的工作流图；约束：无额外运行时限制。
    - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
    - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：须通过方法内校验；失败条件见下方异常说明。
    - `lease`（LeaseGrant）：当前后台作业持有的租约授权及执行代次；约束：无额外运行时限制。
    - `user_message`（str）：本次 Run 对应的用户原始消息；约束：可省略，默认值为 `''`。
    - `message_id`（str）：关联消息的稳定标识；None 表示未绑定消息；约束：可省略，默认值为 `''`。
    - `run_sequence`（int）：会话内 Run 的单调递增序号；约束：可省略，默认值为 `1`。
    - `attachment_ids`（list[str] | None）：当前请求已授权使用的附件标识集合；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
    - `resume_value`（RootPlanCompletedResumeValue | RootPlanCheckpointRecovery | Mapping[str, Any] | None）：图中断后用于恢复执行的持久化值；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`；须通过方法内校验；失败条件见下方异常说明。
    - `stop_signal`（asyncio.Event | None）：通知当前执行应停止的异步信号；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

    返回：
    - Mapping | dict[str, str] | Any：Root 图本次调用生成的状态映射。

    异常：
    - ValueError：校验条件 `not run_id` 成立时（异常说明：run_id is required）。
    - RuntimeError：校验条件 `resume_value is not None；isinstance(resume_value, Mapping)；捕获 ValidationError 后` 成立时（异常说明：root_resume_value_type_invalid）。
    - RuntimeError：校验条件 `resume_value 不符合已定义的恢复类型` 成立时（异常说明：root_resume_value_type_invalid）。
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
                if resume_value.get("type") == "plan_checkpoint_recovery":
                    resume_value = RootPlanCheckpointRecovery(**dict(resume_value))
                elif resume_value.get("type") == "plan_completed":
                    resume_value = RootPlanCompletedResumeValue(**dict(resume_value))
                else:
                    raise ValueError("root_resume_value_type_invalid")
            except ValidationError as error:
                raise RuntimeError("root_resume_value_type_invalid") from error
            except ValueError as error:
                raise RuntimeError("root_resume_value_type_invalid") from error
        if isinstance(resume_value, RootPlanCheckpointRecovery):
            logger.info(f"Recovering root checkpoint for run {run_id}")
            # 已消费的 interrupt 不再 resume；先把当前调用租约写回原 checkpoint，再从游标继续。
            await graph.aupdate_state(
                config,
                {
                    "execution_lease": json_value(lease),
                    "resume_value": json_value(resume_value),
                },
            )
            result = await graph.ainvoke(
                None, config=config
            )
        elif isinstance(resume_value, RootPlanCompletedResumeValue):
            logger.info(
                f"Resuming root graph for run {run_id} from plan {resume_value.plan_id}"
            )
            result = await graph.ainvoke(
                Command(
                    update={
                        "execution_lease": json_value(lease),
                        "resume_value": json_value(resume_value),
                    },
                    resume=json_value(resume_value),
                ),
                config=config,
            )
        else:
            raise RuntimeError("root_resume_value_type_invalid")
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
