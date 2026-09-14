"""中心编排在主图中的分支节点。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import StateNode

from ..state import AgentState
from ..execution_loop.finish import finish_execution
from ..execution_loop._helpers import _stop_signal

if TYPE_CHECKING:
    from ..graph_runtime import LangGraphRuntime

async def materialize_plan(
    state: AgentState, *, runtime: LangGraphRuntime
) -> AgentState:
    """校验并持久化顶层 LLM 提交的计划，不再调用模型。"""
    request = state.get("plan_request")
    if not request:
        raise ValueError("materialize_plan requires plan_request")
    plan = await runtime.materialize_plan(
        session_id=state.get("session_id", ""),
        root_run_id=state.get("run_id", ""),
        user_goal=state.get("user_message", ""),
        submission=request,
    )
    return {"execution_plan": plan.model_dump(mode="json")}


async def execute_plan(
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
        ValueError: Planner 决策缺少计划。
    """
    plan_payload = state.get("execution_plan")
    if plan_payload is None:
        raise ValueError("execute_plan route requires a persisted plan")

    result = await runtime.execute_planned_orchestration(
        plan_payload=plan_payload,
        session_id=state.get("session_id", ""),
        stop_signal=_stop_signal(config),
    )
    return {"orchestration_result": result}


async def close_plan_stream(
    state: AgentState,
    config: RunnableConfig,
    *,
    runtime: LangGraphRuntime,
) -> AgentState:
    """计划汇总完成后关闭顶层 Agent 决策打开的回答流。"""
    execution = state.get("execution", {})
    return {
        "execution": await finish_execution(
            execution, config, graph_runtime=runtime
        )
    }


async def synthesize_orchestration(
    state: AgentState, *, runtime: LangGraphRuntime
) -> AgentState:
    """把 Worker 结果转换为最终响应 payload。

    参数：
        state: 包含 ``orchestration_result`` 的主图状态。
        runtime: 提供编排依赖的运行时。

    返回值：
        AgentState: 包含最终 ``result`` 的 JSON 安全状态。

    异常：
        ValueError: 编排结果缺少必要字段。
    """
    orchestration = state.get("orchestration_result") or {}
    content = orchestration.get("content")
    if content is None:
        raise ValueError("orchestration result missing content")
    return {
        "result": {
            "content": content,
            "run_id": state.get("run_id", ""),
            "turn_count": 0,
            "tool_results": [],
            "error": None,
            "interrupted": False,
            "attachments": [],
            "plan_id": orchestration.get("plan_id"),
            "worker_results": orchestration.get("results", {}),
        }
    }


def create_materialize_plan_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    """创建纯校验/持久化计划节点。"""

    async def node(state: AgentState) -> AgentState:
        return await materialize_plan(state, runtime=runtime)

    return node


def create_execute_plan_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    """创建计划执行节点。"""

    async def node(state: AgentState, config: RunnableConfig) -> AgentState:
        return await execute_plan(state, config, runtime=runtime)

    return node


def create_close_plan_stream_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    """创建计划分支的回答流收尾节点。"""

    async def node(state: AgentState, config: RunnableConfig) -> AgentState:
        return await close_plan_stream(state, config, runtime=runtime)

    return node


def create_synthesize_orchestration_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    """创建编排汇总节点。"""

    async def node(state: AgentState) -> AgentState:
        return await synthesize_orchestration(state, runtime=runtime)

    return node
