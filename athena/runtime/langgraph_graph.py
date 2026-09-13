"""Agent Runtime 使用的 LangGraph 图定义。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .nodes import (
    check_file_results,
    decide_memory_request,
    create_handle_file_failure_node,
    create_agent_loop_node,
    create_post_process_turn_node,
    route_after_agent_loop,
    create_finalize_response_node,
    create_prepare_context_node,
    create_prepare_and_persist_request_node,
    create_process_attachments_node,
    create_retrieve_memory_node,
    create_decide_memory_node,
    create_execute_plan_node,
    create_plan_orchestration_node,
    create_synthesize_orchestration_node,
    route_after_planning,
)
from .state import AgentState

if TYPE_CHECKING:
    from .graph_runtime import LangGraphRuntime


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
    # 请求准备 → 使用 RequestService
    graph.add_node(
        "prepare_and_persist_request",
        create_prepare_and_persist_request_node(runtime.request_service),
    )
    # 附件处理 → 使用完整 runtime（需要 FileIntelligenceRuntime 等）
    graph.add_node("process_attachments", create_process_attachments_node(runtime))
    # 文件失败终点 → 无依赖
    graph.add_node("handle_file_failure", create_handle_file_failure_node())
    # 记忆决策 → 使用 MemoryService
    graph.add_node("decide_memory", create_decide_memory_node(runtime.memory_service))
    # 记忆检索 → 使用 MemoryService
    graph.add_node(
        "retrieve_memory", create_retrieve_memory_node(runtime.memory_service)
    )
    # 上下文准备 → 使用 RequestService
    graph.add_node(
        "prepare_context", create_prepare_context_node(runtime.request_service)
    )
    graph.add_node("plan_orchestration", create_plan_orchestration_node(runtime))
    graph.add_node("execute_plan", create_execute_plan_node(runtime))
    graph.add_node(
        "synthesize_orchestration",
        create_synthesize_orchestration_node(runtime),
    )
    # Agent 循环（LLM ↔ 工具）→ 使用完整 runtime
    graph.add_node("agent_loop", create_agent_loop_node(runtime))
    # 后处理 → 使用 ExecutionService
    graph.add_node(
        "post_process_turn", create_post_process_turn_node(runtime.execution_service)
    )
    # 收尾 → 无依赖
    graph.add_node("finalize_response", create_finalize_response_node())

    # ── 边 ──
    graph.add_edge(START, "prepare_and_persist_request")
    graph.add_edge("prepare_and_persist_request", "process_attachments")
    graph.add_conditional_edges(
        "process_attachments",
        check_file_results,
        {
            "decide_memory": "decide_memory",
            "handle_file_failure": "handle_file_failure",
        },
    )
    graph.add_edge("handle_file_failure", "finalize_response")
    graph.add_conditional_edges(
        "decide_memory",
        decide_memory_request,
        {"retrieve_memory": "retrieve_memory", "prepare_context": "prepare_context"},
    )
    graph.add_edge("retrieve_memory", "prepare_context")
    graph.add_edge("prepare_context", "plan_orchestration")

    graph.add_conditional_edges(
        "plan_orchestration",
        route_after_planning,
        {"agent_loop": "agent_loop", "execute_plan": "execute_plan"},
    )
    graph.add_conditional_edges(
        "agent_loop",
        route_after_agent_loop,
        {
            "post_process_turn": "post_process_turn",
            "finalize_response": "finalize_response",
        },
    )
    graph.add_edge("execute_plan", "synthesize_orchestration")
    graph.add_edge("synthesize_orchestration", "finalize_response")

    graph.add_edge("post_process_turn", "finalize_response")
    graph.add_edge("finalize_response", END)
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

    参数:
        graph: 已由 ``build_graph`` 编译的图对象。
        session_id: 非空会话 ID，用于加载会话级历史和附件。
        user_message: 用户输入文本。
        run_id: 非空运行 ID，同时作为 LangGraph 检查点线程 ID。
        attachment_ids: 可选附件 ID 列表。
        stop_signal: 可选停止信号；置位后终止当前 Harness 和子 Agent 执行。
    返回值:
        Any: 图最终产生的业务结果。
    异常:
        RuntimeError: 图状态包含错误信息时抛出。
    """
    if not run_id:
        raise ValueError("run_id is required for LangGraph checkpoint identity")
    stop_signal = stop_signal or asyncio.Event()

    config = {
        "configurable": {
            "thread_id": run_id,
            "stop_signal": stop_signal,
        },
        "metadata": {
            "session_id": session_id,
            "run_id": run_id,
        },
    }
    get_state = getattr(graph, "aget_state", None)
    snapshot = await get_state(config) if get_state is not None else None
    has_checkpoint = bool(getattr(snapshot, "values", None)) and bool(
        getattr(snapshot, "next", ())
    )
    if (
        get_state is not None
        and getattr(snapshot, "values", None)
        and not getattr(snapshot, "next", ())
    ):
        values = snapshot.values
        if values.get("error"):
            raise RuntimeError(values["error"])
        return values.get("result")
    initial_state: AgentState | None = None
    if not has_checkpoint:
        initial_state = {
            "session_id": session_id,
            "run_id": run_id,
            "message_id": message_id,
            "user_message": user_message,
            "attachment_ids": attachment_ids or [],
        }
    state = await graph.ainvoke(initial_state, config=config)
    if state.get("error"):
        raise RuntimeError(state["error"])
    return state.get("result")
