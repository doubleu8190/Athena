"""Task Understanding 与 Context Provider 的 LangGraph 节点。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langgraph.graph.state import StateNode

from ..state import AgentState

if TYPE_CHECKING:
    from ..graph_runtime import LangGraphRuntime


async def understand_task(
    state: AgentState, *, runtime: LangGraphRuntime
) -> AgentState:
    """生成当前请求的任务理解结果。"""
    if not state.get("session_id"):
        raise ValueError("AgentState missing required field: session_id")
    return await runtime.understand_task(state)


async def plan_context(
    state: AgentState, *, runtime: LangGraphRuntime
) -> AgentState:
    """根据任务理解结果生成上下文获取计划。"""
    if not state.get("session_id"):
        raise ValueError("AgentState missing required field: session_id")
    return runtime.plan_context(state)


async def acquire_context(
    state: AgentState, *, runtime: LangGraphRuntime
) -> AgentState:
    """并发获取计划中的上下文。"""
    if not state.get("session_id"):
        raise ValueError("AgentState missing required field: session_id")
    return await runtime.acquire_context(state)


async def clarification_response(
    state: AgentState, *, runtime: LangGraphRuntime
) -> AgentState:
    """把澄清问题作为普通回答持久化并完成当前 run。"""
    if not state.get("session_id"):
        raise ValueError("AgentState missing required field: session_id")
    return await runtime.complete_clarification(state)


def create_understand_task_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    async def node(state: AgentState) -> AgentState:
        return await understand_task(state, runtime=runtime)

    return node


def create_plan_context_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    async def node(state: AgentState) -> AgentState:
        return await plan_context(state, runtime=runtime)

    return node


def create_acquire_context_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    async def node(state: AgentState) -> AgentState:
        return await acquire_context(state, runtime=runtime)

    return node


def create_clarification_response_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    async def node(state: AgentState) -> AgentState:
        return await clarification_response(state, runtime=runtime)

    return node
