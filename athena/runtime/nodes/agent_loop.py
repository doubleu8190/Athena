"""主图集成 — 将执行循环子图嵌入外层 Pipeline。

Agent loop 本体已拆至 ``agent_runtime/execution_loop/`` 模块。
本文件仅保留外层集成：子图创建、执行后路由和后处理。
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Literal

from langgraph.graph.state import CompiledStateGraph, StateNode

from ..state import AgentState
from ..execution_loop.graph import build_agent_loop

if TYPE_CHECKING:
    from ..langgraph_runtime import LangGraphRuntime
    from ..services.agent_execution_service import AgentExecutionService


def create_agent_loop_node(runtime: LangGraphRuntime) -> CompiledStateGraph:
    """创建主图中使用的 Agent loop 子图。

    参数：
        runtime: 依赖注入的运行时，传入 execution_loop 以构建子图。

    返回值：
        CompiledStateGraph: 已编译的 LLM/工具循环子图。
    """
    return build_agent_loop(runtime)


def route_after_agent_loop(
    state: AgentState,
) -> Literal[
    "post_process_and_build_result",
    "materialize_execution_plan",
    "assemble_final_response",
]:
    """选择执行完成后的主图阶段。

    若子图产出了 ``harness_result`` 则进入后处理，否则直接收尾。
    """
    if state.get("orchestration", {}).get("plan_request") is not None:
        return "materialize_execution_plan"
    return (
        "post_process_and_build_result"
        if state.get("response", {}).get("harness_result") is not None
        else "assemble_final_response"
    )


async def post_process_and_build_result(
    state: AgentState,
    *,
    agent_execution_service: AgentExecutionService,
) -> AgentState:
    """执行完成后的记忆/摘要后处理。

    参数：
        state: 至少包含 ``harness_result`` 的图状态。
        agent_execution_service: 提供后处理和结果序列化能力的服务。

    返回值：
        AgentState: 仅包含 ``result`` 字段。
    """
    request = state.get("request", {})
    if not request.get("session_id"):
        raise ValueError("AgentState missing required field: session_id")
    payload = state.get("response", {}).get("harness_result")
    if payload is None:
        return {}
    from athena.runtime.execution_loop.results import AgentExecutionResult
    from ..services.agent_execution_service import AgentExecutionService

    result = AgentExecutionResult(**payload)
    attachment_refs = AgentExecutionService.deserialize_attachment_refs(
        state.get("context", {}).get("attachment_refs", [])
    )
    if result.error is None and not result.interrupted:
        await agent_execution_service.process_completed_run(
            request.get("session_id", ""),
            request.get("user_message", ""),
            result,
            turn_id=request.get("run_id", ""),
        )
    return {
        "response": {
            "result": AgentExecutionService.build_result_payload(result, attachment_refs)
        }
    }


def create_post_process_and_build_result_node(
    agent_execution_service: AgentExecutionService,
) -> StateNode[AgentState, None]:
    """创建执行结果后处理节点。

    参数：
        agent_execution_service: 提供完成后处理和结果序列化能力的服务。

    返回值：
        StateNode: 可注册到 LangGraph 的异步节点。
    """
    return partial(
        post_process_and_build_result,
        agent_execution_service=agent_execution_service,
    )
