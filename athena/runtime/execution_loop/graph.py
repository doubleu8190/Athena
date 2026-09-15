"""构建可检查点化的 LLM ↔ 工具执行循环子图。"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ..state import AgentExecutionState, AgentState

from .initialize import initialize_execution
from .llm_call import llm_call
from .execute_tools import execute_tool_batch
from .finish import finish_execution

if TYPE_CHECKING:
    from ..graph_runtime import LangGraphRuntime

AgentLoopRoute = Literal[
    "llm_call", "execute_tool_batch", "finish_execution", "plan_requested"
]

# ── 主图适配器（包装 AgentExecutionState → AgentState） ──


async def _initialize_wrapper(
    state: AgentState,
    *,
    graph_runtime: LangGraphRuntime,
) -> AgentState:
    """将主图状态映射为子图内部的执行状态。"""
    exec_state: AgentExecutionState = state.get("execution", {})
    return {
        "execution": initialize_execution(
            {
                "session_id": state.get("session_id", exec_state.get("session_id", "")),
                "run_id": state.get("run_id", exec_state.get("run_id", "")),
                "message_id": state.get("message_id", exec_state.get("message_id", "")),
                "user_message": state.get(
                    "user_message", exec_state.get("user_message", "")
                ),
                "memory_context": state.get(
                    "memory_context", exec_state.get("memory_context", "")
                ),
                "system_prompt": graph_runtime.build_system_prompt(
                    state.get("memory_context", "")
                ),
                "messages": list(
                    state.get("harness_messages", exec_state.get("messages", []))
                ),
                "attachment_refs": list(
                    state.get("attachment_refs", exec_state.get("attachment_refs", []))
                ),
                "max_turns": graph_runtime._settings.max_turns_per_run,
                "max_retries": graph_runtime._settings.retry_budget,
            }
        )
    }


async def _llm_wrapper(
    state: AgentState,
    config,
    *,
    graph_runtime: LangGraphRuntime,
) -> AgentState:
    return {
        "execution": await llm_call(
            state.get("execution", {}), config, graph_runtime=graph_runtime
        )
    }


def _route_after_llm(state: AgentState) -> AgentLoopRoute:
    exec_state: AgentExecutionState = state.get("execution", {})
    if exec_state.get("interrupted"):
        return "finish_execution"
    if exec_state.get("route") == "plan_requested":
        return "plan_requested"
    if exec_state.get("pending_tool_calls"):
        return "execute_tool_batch"
    if exec_state.get("error"):
        # 协议错误（例如无效的 submit_plan）明确不可重试；不能仅依据
        # retry_count/max_retries 再次调用 LLM，否则模型可能继续产生错误调用。
        if not exec_state.get("retryable", False):
            return "finish_execution"
        if int(exec_state.get("turn_count", 0)) < int(
            exec_state.get("max_turns", 20)
        ) and int(exec_state.get("retry_count", 0)) < int(
            exec_state.get("max_retries", 3)
        ):
            return "llm_call"
    return "finish_execution"


async def _tools_wrapper(
    state: AgentState,
    config,
    *,
    graph_runtime: LangGraphRuntime,
) -> AgentState:
    return {
        "execution": await execute_tool_batch(
            state.get("execution", {}), config, graph_runtime=graph_runtime
        )
    }


def _route_after_tools(state: AgentState) -> AgentLoopRoute:
    exec_state: AgentExecutionState = state.get("execution", {})
    return (
        "finish_execution"
        if exec_state.get("interrupted") or exec_state.get("error")
        else "llm_call"
    )


async def _finish_wrapper(
    state: AgentState,
    config,
    *,
    graph_runtime: LangGraphRuntime,
) -> AgentState:
    result = await finish_execution(
        state.get("execution", {}), config, graph_runtime=graph_runtime
    )
    return {
        "execution": result,
        "harness_result": result.get("harness_result"),
        "error": (result.get("harness_result") or {}).get("error"),
        "error_detail": (result.get("harness_result") or {}).get("error_detail"),
    }


async def _plan_requested_wrapper(
    state: AgentState,
    config,
    *,
    graph_runtime: LangGraphRuntime,
) -> AgentState:
    """把顶层 Agent 的计划请求交回外层主图，计划分支统一负责收尾。"""
    execution = state.get("execution", {})
    return {
        "execution": execution,
        "plan_request": execution.get("plan_request"),
    }


# ── 图构建 ──


def build_agent_loop(runtime: LangGraphRuntime) -> CompiledStateGraph:
    """构建可检查点化的 LLM/工具循环子图。

    图结构：
        START → initialize → llm_call ⇄ execute_tool_batch → finish → END
    """
    graph = StateGraph(AgentState)
    graph.add_node(
        "initialize_execution",
        partial(_initialize_wrapper, graph_runtime=runtime),
    )
    graph.add_node("llm_call", partial(_llm_wrapper, graph_runtime=runtime))
    graph.add_node(
        "execute_tool_batch",
        partial(_tools_wrapper, graph_runtime=runtime),
    )
    graph.add_node("finish_execution", partial(_finish_wrapper, graph_runtime=runtime))
    graph.add_node(
        "plan_requested",
        partial(_plan_requested_wrapper, graph_runtime=runtime),
    )
    graph.add_edge(START, "initialize_execution")
    graph.add_edge("initialize_execution", "llm_call")
    graph.add_conditional_edges(
        "llm_call",
        _route_after_llm,
        {
            "llm_call": "llm_call",
            "execute_tool_batch": "execute_tool_batch",
            "finish_execution": "finish_execution",
            "plan_requested": "plan_requested",
        },
    )
    graph.add_conditional_edges(
        "execute_tool_batch",
        _route_after_tools,
        {"llm_call": "llm_call", "finish_execution": "finish_execution"},
    )
    graph.add_edge("finish_execution", END)
    graph.add_edge("plan_requested", END)
    return graph.compile()
