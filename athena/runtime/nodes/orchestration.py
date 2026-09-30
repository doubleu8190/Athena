"""中心编排在主图中的分支节点。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import StateNode

from ..state import AgentState
from ..execution_loop.finish import finish_execution
from ..execution_loop._helpers import _stop_signal

if TYPE_CHECKING:
    from ..langgraph_runtime import LangGraphRuntime

async def materialize_execution_plan(
    state: AgentState, *, runtime: LangGraphRuntime
) -> AgentState:
    """校验并持久化顶层 LLM 提交的计划，不再调用模型。"""
    orchestration = state.get("orchestration", {})
    request_state = state.get("request", {})
    request = orchestration.get("plan_request")
    if not request:
        raise ValueError("materialize_execution_plan requires plan_request")
    plan = await runtime.materialize_plan(
        session_id=request_state.get("session_id", ""),
        root_run_id=request_state.get("run_id", ""),
        user_goal=request_state.get("user_message", ""),
        submission=request,
    )
    return {
        "phase": "orchestration",
        "orchestration": {
            **orchestration,
            "execution_plan": plan.model_dump(mode="json"),
        },
    }


async def run_planned_orchestration(
    state: AgentState,
    config: RunnableConfig,
    *,
    runtime: LangGraphRuntime,
) -> AgentState:
    """执行已规划的无依赖任务并生成汇总结果。

    参数：
        state: 包含规划决策的主图状态。
        config: LangGraph 配置，包含会话停止信号。
        runtime: 提供编排依赖的运行时。

    返回值：
        AgentState: 包含 ``orchestration_result`` 的 JSON 安全状态。

    异常：
        ValueError: 顶层 Agent 的决策缺少计划。
    """
    orchestration = state.get("orchestration", {})
    request_state = state.get("request", {})
    plan_payload = orchestration.get("execution_plan")
    if plan_payload is None:
        raise ValueError("run_planned_orchestration route requires a persisted plan")

    result = await runtime.execute_planned_orchestration(
        plan_payload=plan_payload,
        session_id=request_state.get("session_id", ""),
        stop_signal=_stop_signal(config),
    )
    return {
        "phase": "response",
        "orchestration": {**orchestration, "orchestration_result": result},
    }


async def close_execution_stream(
    state: AgentState,
    config: RunnableConfig,
    *,
    runtime: LangGraphRuntime,
) -> AgentState:
    """计划汇总完成后关闭顶层 Agent 决策打开的回答流。"""
    execution = state.get("execution", {})
    request_state = state.get("request", {})
    response = state.get("response", {})
    return {
        "execution": await finish_execution(
            execution,
            config,
            graph_runtime=runtime,
            session_id=request_state.get("session_id", ""),
            run_id=request_state.get("run_id", ""),
            message_id=request_state.get("message_id"),
            error=response.get("error"),
            error_detail=response.get("error_detail"),
        )
    }


async def build_orchestration_response(
    state: AgentState,
) -> AgentState:
    """把 Worker 结果转换为最终响应 payload。

    参数：
        state: 包含 ``orchestration_result`` 的主图状态。

    返回值：
        AgentState: 包含最终 ``result`` 的 JSON 安全状态。

    异常：
        ValueError: 编排结果缺少必要字段。
    """
    orchestration_state = state.get("orchestration", {})
    request_state = state.get("request", {})
    orchestration = orchestration_state.get("orchestration_result") or {}
    content = orchestration.get("content")
    if content is None:
        raise ValueError("orchestration result missing content")
    return {
        "phase": "response",
        "response": {"result": {
            "content": content,
            "run_id": request_state.get("run_id", ""),
            "turn_count": 0,
            "tool_results": [],
            "error": None,
            "interrupted": False,
            "attachments": [],
            "plan_id": orchestration.get("plan_id"),
            "worker_results": orchestration.get("results", {}),
        }}
    }


def create_materialize_execution_plan_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    """创建纯校验/持久化计划节点。"""

    async def node(state: AgentState) -> AgentState:
        return await materialize_execution_plan(state, runtime=runtime)

    return node


def create_run_planned_orchestration_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    """创建计划执行节点。"""

    async def node(state: AgentState, config: RunnableConfig) -> AgentState:
        return await run_planned_orchestration(state, config, runtime=runtime)

    return node


def create_close_execution_stream_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    """创建计划分支的回答流收尾节点。"""

    async def node(state: AgentState, config: RunnableConfig) -> AgentState:
        return await close_execution_stream(state, config, runtime=runtime)

    return node


def create_build_orchestration_response_node() -> StateNode[AgentState, None]:
    """创建编排结果响应适配节点。"""

    async def node(state: AgentState) -> AgentState:
        return await build_orchestration_response(state)

    return node
