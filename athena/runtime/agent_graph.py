"""Agent Runtime 使用的 LangGraph 图定义。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph, RunnableConfig

from athena.utils.logging import get_logger

from .nodes import (
    route_after_attachment_processing,
    route_after_task_understanding,
    create_handle_attachment_failure_node,
    create_agent_loop_node,
    create_post_process_and_build_result_node,
    route_after_agent_loop,
    create_assemble_final_response_node,
    create_prepare_harness_input_node,
    create_prepare_request_and_persist_message_node,
    create_process_attachments_node,
    create_understand_task_node,
    create_plan_context_node,
    create_acquire_context_node,
    create_clarification_response_node,
    create_run_planned_orchestration_node,
    create_materialize_execution_plan_node,
    create_build_orchestration_response_node,
    create_close_execution_stream_node,
)
from .state import AgentState
from .node_events import instrument_graph_node

if TYPE_CHECKING:
    from .langgraph_runtime import LangGraphRuntime

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
    # 所有主图节点都在注册边界统一包装，保证普通节点和嵌套 Agent loop
    # 具有一致的 started/completed/failed 事件生命周期。
    event_publisher = getattr(runtime, "_events", None)

    def add_instrumented_node(node_name: str, node: Any) -> None:
        """注册一个带节点生命周期事件的主图节点。"""
        graph.add_node(
            node_name,
            instrument_graph_node(
                node_name,
                node,
                event_publisher=event_publisher,
            ),
        )

    # ── 节点：按执行顺序注册 ──
    # 请求准备并持久化消息 → 使用 SessionContextService
    add_instrumented_node(
        "prepare_request_and_persist_message",
        create_prepare_request_and_persist_message_node(
            runtime.session_context_service
        ),
    )
    # 附件处理 → 使用完整 runtime（需要 FileIntelligenceRuntime 等）
    add_instrumented_node("process_attachments", create_process_attachments_node(runtime))
    # 附件失败处理 → 无依赖
    add_instrumented_node("handle_attachment_failure", create_handle_attachment_failure_node())
    # 任务理解 → 生成 UserTaskSpec
    add_instrumented_node("understand_task", create_understand_task_node(runtime))
    # 上下文计划 → 推导 Provider
    add_instrumented_node("plan_context", create_plan_context_node(runtime))
    # 上下文获取 → 并发调用 Provider
    add_instrumented_node("acquire_context", create_acquire_context_node(runtime))
    # 澄清回答 → 持久化为普通 assistant 回答并结束 run
    add_instrumented_node(
        "clarification_response", create_clarification_response_node(runtime)
    )
    # 准备 Harness 输入 → 使用 SessionContextService
    add_instrumented_node(
        "prepare_harness_input",
        create_prepare_harness_input_node(runtime.session_context_service),
    )
    # Agent 循环（LLM ↔ 工具）→ 使用完整 runtime
    add_instrumented_node("agent_loop", create_agent_loop_node(runtime))
    add_instrumented_node(
        "materialize_execution_plan", create_materialize_execution_plan_node(runtime)
    )
    add_instrumented_node(
        "run_planned_orchestration", create_run_planned_orchestration_node(runtime)
    )
    add_instrumented_node(
        "build_orchestration_response",
        create_build_orchestration_response_node(),
    )
    add_instrumented_node(
        "close_execution_stream", create_close_execution_stream_node(runtime)
    )

    # 后处理并组装 Harness 结果 → 使用 AgentExecutionService
    add_instrumented_node(
        "post_process_and_build_result",
        create_post_process_and_build_result_node(runtime.agent_execution_service),
    )
    # 组装最终响应 → 无依赖
    add_instrumented_node("assemble_final_response", create_assemble_final_response_node())

    # ── 边 ──
    graph.add_edge(START, "prepare_request_and_persist_message")
    graph.add_edge("prepare_request_and_persist_message", "process_attachments")
    graph.add_conditional_edges(
        "process_attachments",
        route_after_attachment_processing,
        {
            "understand_task": "understand_task",
            "handle_attachment_failure": "handle_attachment_failure",
        },
    )
    graph.add_edge("handle_attachment_failure", "assemble_final_response")
    graph.add_edge("understand_task", "plan_context")
    graph.add_conditional_edges(
        "understand_task",
        route_after_task_understanding,
        {
            "clarification_response": "clarification_response",
            "plan_context": "plan_context",
        },
    )
    graph.add_edge("clarification_response", "assemble_final_response")
    graph.add_edge("plan_context", "acquire_context")
    graph.add_edge("acquire_context", "prepare_harness_input")
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
