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
    from ..graph_runtime import LangGraphRuntime
    from ..services.execution_service import ExecutionService


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
) -> Literal["post_process_turn", "materialize_plan", "finalize_response"]:
    """选择执行完成后的主图阶段。

    若子图产出了 ``harness_result`` 则进入后处理，否则直接收尾。
    """
    if state.get("plan_request") is not None:
        return "materialize_plan"
    return "post_process_turn" if state.get("harness_result") is not None else "finalize_response"


async def post_process_turn(
    state: AgentState,
    *,
    execution_service: ExecutionService,
) -> AgentState:
    """执行完成后的记忆/摘要后处理。

    参数：
        state: 至少包含 ``harness_result`` 的图状态。
        execution_service: 提供后处理和结果序列化能力的服务。

    返回值：
        AgentState: 仅包含 ``result`` 字段。
    """
    if not state.get("session_id"):
        raise ValueError("AgentState missing required field: session_id")
    payload = state.get("harness_result")
    if payload is None:
        return {}
    from athena.core.harness.harness import HarnessRunResult
    from ..services.execution_service import ExecutionService

    result = HarnessRunResult(**payload)
    attachment_refs = ExecutionService.deserialize_attachment_refs(
        state.get("attachment_refs", [])
    )
    if result.error is None and not result.interrupted:
        await execution_service.post_process(
            state.get("session_id", ""),
            state.get("user_message", ""),
            result,
            turn_id=state.get("run_id", ""),
        )
    return {"result": ExecutionService.result_payload(result, attachment_refs)}


def create_post_process_turn_node(
    execution_service: ExecutionService,
) -> StateNode[AgentState, None]:
    """创建执行结果后处理节点。

    参数：
        execution_service: 提供 post_process 和 result_payload 能力的服务。

    返回值：
        StateNode: 可注册到 LangGraph 的异步节点。
    """
    return partial(post_process_turn, execution_service=execution_service)
