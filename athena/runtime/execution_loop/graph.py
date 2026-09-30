"""构建可检查点化的 LLM ↔ 工具执行循环子图。"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ..state import AgentExecutionState, AgentState
from ..node_events import instrument_graph_node

from .initialize import initialize_execution
from .llm_call import llm_call
from .prepare_tools import prepare_tool_batch
from .approval_gate import approval_gate
from .dispatch_tools import dispatch_tool_calls
from .tool_call import tool_call
from .collect_tools import collect_tool_results
from .finish import finish_execution

if TYPE_CHECKING:
    from ..langgraph_runtime import LangGraphRuntime

AgentLoopRoute = Literal[
    "llm_call", "prepare_tool_batch", "approval_gate", "tool_call",
    "collect_tool_results", "finish_execution", "plan_requested"
]

# ── 主图适配器（包装 AgentExecutionState → AgentState） ──


async def _initialize_wrapper(
    state: AgentState,
    *,
    graph_runtime: LangGraphRuntime,
) -> AgentState:
    """将阶段状态映射为执行循环状态。"""
    execution: AgentExecutionState = state.get("execution", {})
    exec_state = execution.get("recoverable", {})
    request = state.get("request", {})
    understanding = state.get("understanding", {})
    context = state.get("context", {})
    return {
        "phase": "agent_loop",
        "execution": initialize_execution(
            {
                "recoverable": {
                    "system_prompt": graph_runtime.build_system_prompt(
                        understanding.get("task_spec"), context.get("context_bundle")
                    ),
                    "messages": list(
                        context.get("harness_messages", exec_state.get("messages", []))
                    ),
                    "max_turns": int(
                        exec_state.get("max_turns", graph_runtime._settings.max_turns_per_run)
                    ),
                    "max_retries": int(
                        exec_state.get("max_retries", graph_runtime._settings.retry_budget)
                    ),
                },
            }
        )
    }


async def _llm_wrapper(
    state: AgentState,
    config,
    *,
    graph_runtime: LangGraphRuntime,
) -> AgentState:
    execution_update, error, error_detail, plan_request = await llm_call(
        state.get("execution", {}),
        config,
        graph_runtime=graph_runtime,
        session_id=state.get("request", {}).get("session_id", ""),
        run_id=state.get("request", {}).get("run_id", ""),
    )
    return {
        "execution": execution_update,
        "response": {"error": error, "error_detail": error_detail},
        "orchestration": {"plan_request": plan_request},
    }


def _route_after_llm(state: AgentState) -> AgentLoopRoute:
    execution: AgentExecutionState = state.get("execution", {})
    recoverable = execution.get("recoverable", {})
    derived = execution.get("derived", {})
    error = state.get("response", {}).get("error")
    if derived.get("interrupted"):
        return "finish_execution"
    if derived.get("route") == "plan_requested":
        return "plan_requested"
    if recoverable.get("pending_tool_calls"):
        return "prepare_tool_batch"
    if error:
        # 只有业务结果错误允许回到 LLM；Provider 技术失败已经在节点内
        # 完成重试，不能再次由 Graph 放大真实请求次数。
        if derived.get("llm_result_status") == "technical_failure":
            return "finish_execution"
        if not derived.get("retryable", False):
            return "finish_execution"
        if int(recoverable.get("turn_count", 0)) < int(
            recoverable.get("max_turns", 20)
        ) and int(recoverable.get("retry_count", 0)) < int(
            recoverable.get("max_retries", 3)
        ):
            return "llm_call"
    return "finish_execution"


async def _prepare_tools_wrapper(
    state: AgentState,
    config,
    *,
    graph_runtime: LangGraphRuntime,
) -> AgentState:
    return {
        "execution": await prepare_tool_batch(
            state.get("execution", {}),
            config,
            graph_runtime=graph_runtime,
            session_id=state.get("request", {}).get("session_id", ""),
            run_id=state.get("request", {}).get("run_id", ""),
        )
    }


async def _approval_gate_wrapper(
    state: AgentState,
    config,
    *,
    graph_runtime: LangGraphRuntime,
) -> AgentState:
    return {
        "execution": await approval_gate(
            state.get("execution", {}),
            config,
            graph_runtime=graph_runtime,
            run_id=state.get("request", {}).get("run_id", ""),
        )
    }


async def _tool_call_wrapper(
    state: AgentState,
    config,
    *,
    graph_runtime: LangGraphRuntime,
) -> AgentState:
    return await tool_call(
        state,
        config,
        graph_runtime=graph_runtime,
        session_id=state.get("request", {}).get("session_id", ""),
        run_id=state.get("request", {}).get("run_id", ""),
        message_id=state.get("request", {}).get("message_id"),
    )


async def _collect_tools_wrapper(
    state: AgentState, config, *, graph_runtime: LangGraphRuntime
) -> AgentState:
    return {"execution": await collect_tool_results(state.get("execution", {}), config)}


async def _finish_wrapper(
    state: AgentState,
    config,
    *,
    graph_runtime: LangGraphRuntime,
) -> AgentState:
    result = await finish_execution(
        state.get("execution", {}),
        config,
        graph_runtime=graph_runtime,
        session_id=state.get("request", {}).get("session_id", ""),
        run_id=state.get("request", {}).get("run_id", ""),
        message_id=state.get("request", {}).get("message_id"),
        error=state.get("response", {}).get("error"),
        error_detail=state.get("response", {}).get("error_detail"),
    )
    execution = state.get("execution", {})
    recoverable = execution.get("recoverable", {})
    derived = execution.get("derived", {})
    harness_result = {
        "content": recoverable.get("final_content", recoverable.get("last_content", "")),
        "run_id": state.get("request", {}).get("run_id", ""),
        "turn_count": recoverable.get("turn_count", 0),
        "tool_results": recoverable.get("tool_results", []),
        "error": state.get("response", {}).get("error"),
        "error_detail": state.get("response", {}).get("error_detail"),
        "interrupted": derived.get("interrupted", False),
    }
    return {
        "phase": "response",
        "execution": result,
        "response": {"harness_result": harness_result},
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
        "phase": "orchestration",
        "execution": execution,
        "orchestration": {"plan_request": state.get("orchestration", {}).get("plan_request")},
    }


# ── 图构建 ──


def build_agent_loop(
    runtime: LangGraphRuntime, checkpointer: object | None = None
) -> CompiledStateGraph:
    """构建可检查点化的 LLM/工具循环子图。

    图结构：
        START → initialize → llm_call → prepare → approval_gate →
        Send(tool_call) → collect → llm_call → finish → END
    """
    graph = StateGraph(AgentState)

    def add_instrumented_node(node_name: str, node: object) -> None:
        """注册一个带生命周期事件的执行循环节点。"""
        graph.add_node(
            node_name,
            instrument_graph_node(
                node_name,
                node,  # type: ignore[arg-type]
                event_publisher=runtime.event_publisher,
            ),
        )

    add_instrumented_node(
        "initialize_execution",
        partial(_initialize_wrapper, graph_runtime=runtime),
    )
    add_instrumented_node("llm_call", partial(_llm_wrapper, graph_runtime=runtime))
    add_instrumented_node("prepare_tool_batch", partial(_prepare_tools_wrapper, graph_runtime=runtime))
    add_instrumented_node("approval_gate", partial(_approval_gate_wrapper, graph_runtime=runtime))
    add_instrumented_node("tool_call", partial(_tool_call_wrapper, graph_runtime=runtime))
    add_instrumented_node("collect_tool_results", partial(_collect_tools_wrapper, graph_runtime=runtime))
    add_instrumented_node(
        "finish_execution", partial(_finish_wrapper, graph_runtime=runtime)
    )
    add_instrumented_node(
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
            "prepare_tool_batch": "prepare_tool_batch",
            "finish_execution": "finish_execution",
            "plan_requested": "plan_requested",
        },
    )
    graph.add_edge("prepare_tool_batch", "approval_gate")
    graph.add_conditional_edges(
        "approval_gate",
        partial(dispatch_tool_calls, graph_runtime=runtime),
    )
    graph.add_edge("tool_call", "collect_tool_results")
    graph.add_edge("collect_tool_results", "llm_call")
    graph.add_edge("finish_execution", END)
    graph.add_edge("plan_requested", END)
    return graph.compile(checkpointer=checkpointer)
