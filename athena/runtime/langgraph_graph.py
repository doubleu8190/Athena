"""Agent Runtime 使用的 LangGraph 图定义。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph, RunnableConfig

from athena.utils.logging import get_logger

from .nodes import (
    route_after_attachment_processing,
    route_after_memory_request,
    create_handle_attachment_failure_node,
    create_agent_loop_node,
    create_post_process_and_build_result_node,
    route_after_agent_loop,
    create_assemble_final_response_node,
    create_prepare_harness_input_node,
    create_prepare_request_and_persist_message_node,
    create_process_attachments_node,
    create_retrieve_memory_node,
    create_build_memory_request_node,
    create_run_planned_orchestration_node,
    create_materialize_execution_plan_node,
    create_build_orchestration_response_node,
    create_close_execution_stream_node,
)
from .state import AgentState

if TYPE_CHECKING:
    from .graph_runtime import LangGraphRuntime

logger = get_logger(__name__)


def build_graph(
    runtime: LangGraphRuntime, checkpointer: Any = None
) -> CompiledStateGraph:
    """构建并编译 Agent 执行图。

    参数:
        runtime: 提供依赖注入的运行时，内部持有各聚焦 service。
        checkpointer: 可选的 LangGraph 检查点保存器，用于恢复中断执行。
    返回值:
        CompiledStateGraph: 已连接并编译完成的 LangGraph 图对象。
    """
    graph = StateGraph(AgentState)

    # ── 节点：按执行顺序注册 ──
    # 请求准备并持久化消息 → 使用 SessionContextService
    graph.add_node(
        "prepare_request_and_persist_message",
        create_prepare_request_and_persist_message_node(
            runtime.session_context_service
        ),
    )
    # 附件处理 → 使用完整 runtime（需要 FileIntelligenceRuntime 等）
    graph.add_node("process_attachments", create_process_attachments_node(runtime))
    # 附件失败处理 → 无依赖
    graph.add_node("handle_attachment_failure", create_handle_attachment_failure_node())
    # 构建记忆检索请求 → 使用 MemoryService
    graph.add_node(
        "build_memory_request", create_build_memory_request_node(runtime.memory_service)
    )
    # 记忆检索 → 使用 MemoryService
    graph.add_node(
        "retrieve_memory", create_retrieve_memory_node(runtime.memory_service)
    )
    # 准备 Harness 输入 → 使用 SessionContextService
    graph.add_node(
        "prepare_harness_input",
        create_prepare_harness_input_node(runtime.session_context_service),
    )
    # Agent 循环（LLM ↔ 工具）→ 使用完整 runtime
    graph.add_node("agent_loop", create_agent_loop_node(runtime))
    graph.add_node(
        "materialize_execution_plan", create_materialize_execution_plan_node(runtime)
    )
    graph.add_node(
        "run_planned_orchestration", create_run_planned_orchestration_node(runtime)
    )
    graph.add_node(
        "build_orchestration_response",
        create_build_orchestration_response_node(),
    )
    graph.add_node(
        "close_execution_stream", create_close_execution_stream_node(runtime)
    )

    # 后处理并组装 Harness 结果 → 使用 ExecutionService
    graph.add_node(
        "post_process_and_build_result",
        create_post_process_and_build_result_node(runtime.execution_service),
    )
    # 组装最终响应 → 无依赖
    graph.add_node("assemble_final_response", create_assemble_final_response_node())

    # ── 边 ──
    graph.add_edge(START, "prepare_request_and_persist_message")
    graph.add_edge("prepare_request_and_persist_message", "process_attachments")
    graph.add_conditional_edges(
        "process_attachments",
        route_after_attachment_processing,
        {
            "build_memory_request": "build_memory_request",
            "handle_attachment_failure": "handle_attachment_failure",
        },
    )
    graph.add_edge("handle_attachment_failure", "assemble_final_response")
    graph.add_conditional_edges(
        "build_memory_request",
        route_after_memory_request,
        {
            "retrieve_memory": "retrieve_memory",
            "prepare_harness_input": "prepare_harness_input",
        },
    )
    graph.add_edge("retrieve_memory", "prepare_harness_input")
    graph.add_edge("prepare_harness_input", "agent_loop")
    graph.add_conditional_edges(
        "agent_loop",
        route_after_agent_loop,
        {
            "post_process_and_build_result": "post_process_and_build_result",
            "materialize_execution_plan": "materialize_execution_plan",
            "assemble_final_response": "assemble_final_response",
        },
    )
    graph.add_edge("materialize_execution_plan", "run_planned_orchestration")
    graph.add_edge("run_planned_orchestration", "build_orchestration_response")
    graph.add_edge("build_orchestration_response", "close_execution_stream")
    graph.add_edge("close_execution_stream", "assemble_final_response")

    graph.add_edge("post_process_and_build_result", "assemble_final_response")
    graph.add_edge("assemble_final_response", END)
    return graph.compile(checkpointer=checkpointer)


async def invoke_graph(
    graph: CompiledStateGraph,
    *,
    session_id: str,
    user_message: str,
    run_id: str = "",
    attachment_ids: list[str] | None = None,
    message_id: str = "",
    stop_signal: asyncio.Event | None = None,
) -> Any:
    """以指定会话配置执行已编译的 Agent 图。

    支持三种执行路径：
    - **全新运行**：无检查点 → 以初始状态启动；
    - **断点续跑**：有未完成的检查点 → 从断点继续（``initial_state=None``）；
    - **已完成**：检查点已终态 → 直接返回历史结果，避免重复执行。
    """
    if not run_id:
        raise ValueError("run_id is required for LangGraph checkpoint identity")

    config: RunnableConfig = {
        "configurable": {
            "thread_id": run_id,
            "stop_signal": stop_signal or asyncio.Event(),
        },
        "metadata": {
            "session_id": session_id,
            "run_id": run_id,
        },
    }

    snapshot = await _try_get_snapshot(graph, config)
    values = getattr(snapshot, "values", None) if snapshot else None
    pending = getattr(snapshot, "next", ()) if snapshot else ()

    # 已终态：直接返回历史结果
    if values and not pending:
        return _unwrap(values)

    # 有断点：initial_state 为 None，由 LangGraph 从检查点恢复
    # 全新运行：构造初始状态
    initial_state = (
        None
        if values and pending
        else _build_initial_state(
            session_id=session_id,
            run_id=run_id,
            message_id=message_id,
            user_message=user_message,
            attachment_ids=attachment_ids,
        )
    )

    state = await graph.ainvoke(initial_state, config=config)
    return _unwrap(state or {})


async def _try_get_snapshot(
    graph: CompiledStateGraph, config: RunnableConfig
) -> Any | None:
    """读取检查点快照；不支持或读取失败时返回 None（视为全新运行）。"""
    get_state = getattr(graph, "aget_state", None)
    if get_state is None:
        return None
    try:
        return await get_state(config)
    except Exception:
        # 首次运行时存储可能未初始化，属正常情况，降级为全新运行
        logger.debug("invoke_graph.get_state_failed", exc_info=True)
        return None


def _build_initial_state(
    *,
    session_id: str,
    run_id: str,
    message_id: str,
    user_message: str,
    attachment_ids: list[str] | None,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "run_id": run_id,
        "message_id": message_id,
        "user_message": user_message,
        # 防御性复制：避免外部修改原列表影响状态
        "attachment_ids": list(attachment_ids or []),
    }


def _unwrap(state: dict[str, Any]) -> Any:
    """统一从状态中提取 error 或 result。"""
    if state.get("error"):
        raise RuntimeError(state["error"])
    return state.get("result")
