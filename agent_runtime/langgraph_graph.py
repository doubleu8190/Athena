"""Agent Runtime 使用的 LangGraph 图定义。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from athena.utils.ids import generate_time_id

from .nodes import (
    check_file_results,
    create_handle_file_failure_node,
    create_execute_tools_and_llm_node,
    create_finalize_response_node,
    create_prepare_context_node,
    create_prepare_request_node,
    create_persist_message_and_attachments_node,
    create_process_attachments_node,
    create_retrieve_memory_node,
)
from .state import AgentState

if TYPE_CHECKING:
    from .graph_runtime import LangGraphRuntime


def build_graph(
    runtime: LangGraphRuntime, checkpointer: Any = None
) -> CompiledStateGraph:
    """构建并编译 Agent 执行图。

    参数:
        runtime (LangGraphRuntime): 提供请求准备、检索、Harness 执行等能力的运行时。
        checkpointer (Any | None): 可选的 LangGraph 检查点保存器，用于恢复中断执行。
    返回值:
        CompiledStateGraph: 已连接并编译完成的 LangGraph 图对象。
    异常:
        运行时构建失败时传播 LangGraph 或依赖对象抛出的异常。
    """
    graph = StateGraph(AgentState)
    graph.add_node("prepare_request", create_prepare_request_node(runtime))
    graph.add_node(
        "persist_message_and_attachments",
        create_persist_message_and_attachments_node(runtime),
    )
    graph.add_node("process_attachments", create_process_attachments_node(runtime))
    graph.add_node("handle_file_failure", create_handle_file_failure_node())
    graph.add_node("retrieve_memory", create_retrieve_memory_node(runtime))
    graph.add_node("prepare_context", create_prepare_context_node(runtime))
    graph.add_node(
        "execute_tools_and_llm", create_execute_tools_and_llm_node(runtime)
    )
    graph.add_node("finalize_response", create_finalize_response_node())
    graph.add_edge(START, "prepare_request")
    graph.add_edge("prepare_request", "persist_message_and_attachments")
    graph.add_edge("persist_message_and_attachments", "process_attachments")
    graph.add_conditional_edges(
        "process_attachments",
        check_file_results,
        {
            "prepare_context": "retrieve_memory",
            "handle_file_failure": "handle_file_failure",
        },
    )
    graph.add_edge("handle_file_failure", "finalize_response")
    graph.add_edge("retrieve_memory", "prepare_context")
    graph.add_edge("prepare_context", "execute_tools_and_llm")
    graph.add_edge("execute_tools_and_llm", "finalize_response")
    graph.add_edge("finalize_response", END)
    return graph.compile(checkpointer=checkpointer)


async def invoke_graph(
    graph: CompiledStateGraph,
    *,
    session_id: str,
    user_message: str,
    run_id: str = "",
    command_id: str = "",
    attachment_ids: list[str] | None = None,
    message_id: str = "",
    stop_signal: asyncio.Event | None = None,
) -> Any:
    """以指定会话配置执行已编译的 Agent 图。

    参数:
        graph (CompiledStateGraph): 已由 ``build_graph`` 编译的图对象。
        session_id (str): 非空会话 ID，用于加载会话级历史和附件。
        user_message (str): 用户输入文本，可为空但通常应包含有效请求。
        run_id (str): 非空运行 ID，同时作为 LangGraph 检查点线程 ID。
        command_id (str): 可选命令 ID，用于幂等追踪。
        attachment_ids (list[str] | None): 可选附件 ID 列表，元素必须为非空字符串。
        stop_signal (asyncio.Event | None): 可选停止信号；置位后终止当前 Harness 和子 Agent 执行。
    返回值:
        Any: 图最终产生的业务结果；无结果时返回 ``None``。
    异常:
        RuntimeError: 图状态包含错误信息时抛出。
        其他异常: 图节点或底层依赖失败时原样传播。
    """
    if not run_id:
        raise ValueError("run_id is required for LangGraph checkpoint identity")

    initial_state: AgentState = {
        "session_id": session_id,
        "run_id": run_id,
        "command_id": command_id,
        "message_id": message_id or generate_time_id(),
        "user_message": user_message,
        "attachment_ids": attachment_ids or [],
    }
    state = await graph.ainvoke(
        initial_state,
        config={
            "configurable": {
                "thread_id": run_id,
                "stop_signal": stop_signal,
            },
            "metadata": {
                "session_id": session_id,
                "run_id": run_id,
                "command_id": command_id,
            },
        },
    )
    if state.get("error"):
        raise RuntimeError(state["error"])
    return state.get("result")
