"""中心编排在主图中的分支节点。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import StateNode

from ..state import AgentState

if TYPE_CHECKING:
    from ..graph_runtime import LangGraphRuntime


async def plan_orchestration(
    state: AgentState, *, runtime: LangGraphRuntime
) -> AgentState:
    """请求 Planner 判断直接回答或生成执行计划。

    参数：
        state: 已完成上下文准备的主图状态。
        runtime: 提供编排依赖的运行时。

    返回值：
        AgentState: 仅包含 ``planning_decision`` 字段。

    异常：
        ValueError: 状态缺少会话、运行标识或用户目标。
    """
    session_id = state.get("session_id", "")
    run_id = state.get("run_id", "")
    goal = state.get("user_message", "")
    if not session_id or not run_id or not goal:
        raise ValueError("orchestration requires session_id, run_id and user_message")

    decision = await runtime.orchestrate_decide(
        session_id=session_id,
        root_run_id=run_id,
        goal=goal,
        memory_context=state.get("memory_context", ""),
    )
    return {
        "planning_decision": {
            "mode": decision.mode,
            "direct_answer": decision.direct_answer,
            "plan": decision.plan.model_dump(mode="json") if decision.plan else None,
        }
    }


def route_after_planning(
    state: AgentState,
) -> Literal["agent_loop", "execute_plan"]:
    """按 Planner 决策选择旧 Agent 循环或中心编排。"""
    decision = state.get("planning_decision") or {}
    return "execute_plan" if decision.get("mode") == "execute_plan" else "agent_loop"


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
    decision = state.get("planning_decision") or {}
    plan_payload = decision.get("plan")
    if plan_payload is None:
        raise ValueError("execute_plan route requires a persisted plan")

    result = await runtime.execute_planned_orchestration(
        plan_payload=plan_payload,
        session_id=state.get("session_id", ""),
        stop_signal=_stop_signal(config),
    )
    return {"orchestration_result": result}


def _stop_signal(config: RunnableConfig):
    """从 LangGraph 配置提取停止事件。"""
    from ..execution_loop._helpers import _stop_signal as extract_stop_signal

    return extract_stop_signal(config)


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


def create_plan_orchestration_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    """创建 Planner 决策节点。"""

    async def node(state: AgentState) -> AgentState:
        return await plan_orchestration(state, runtime=runtime)

    return node


def create_execute_plan_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    """创建计划执行节点。"""

    async def node(state: AgentState, config: RunnableConfig) -> AgentState:
        return await execute_plan(state, config, runtime=runtime)

    return node


def create_synthesize_orchestration_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    """创建编排汇总节点。"""

    async def node(state: AgentState) -> AgentState:
        return await synthesize_orchestration(state, runtime=runtime)

    return node
