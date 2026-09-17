"""单次 LLM 调用节点。

接收 ``AgentExecutionState``，通过 ``HarnessTurnExecutor`` 执行一次
LLM 调用，将结果（内容、工具调用、错误等）回写为增量状态更新。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig

from ..state import AgentExecutionState
from ._helpers import _stop_signal, _executor

if TYPE_CHECKING:
    from ..langgraph_runtime import LangGraphRuntime


async def llm_call(
    state: AgentExecutionState,
    config: RunnableConfig,
    *,
    graph_runtime: LangGraphRuntime,
) -> AgentExecutionState:
    """执行恰好一次 LLM 调用。"""
    harness = _executor(graph_runtime)
    outcome = await harness.run_llm_turn(
        messages=state.get("messages", []),
        session_id=state.get("session_id", ""),
        run_id=state.get("run_id", ""),
        system_prompt=state.get("system_prompt", ""),
        turn_count=int(state.get("turn_count", 0)),
        retry_count=int(state.get("retry_count", 0)),
        max_turns=int(state.get("max_turns", 20)),
        max_retries=int(state.get("max_retries", 3)),
        stop_signal=_stop_signal(config),
        stream_started=bool(state.get("stream_started", False)),
        stream_version=int(state.get("stream_version", 0)),
        stream_offset=int(state.get("stream_offset", 0)),
        parent_run_id=state.get("parent_run_id"),
        tool_names=state.get("tool_names"),
    )
    status = (
        "interrupted"
        if outcome.interrupted
        else ("failed" if outcome.error and not outcome.retryable else "running")
    )
    return {
        "messages": outcome.messages,
        "pending_tool_calls": outcome.tool_calls,
        "turn_count": outcome.turn_count,
        "retry_count": outcome.retry_count,
        "last_content": outcome.content,
        "final_content": outcome.content or state.get("final_content", ""),
        "error": outcome.error,
        "error_detail": outcome.error_detail,
        "retryable": outcome.retryable,
        "interrupted": outcome.interrupted,
        "stream_started": outcome.stream_started,
        "stream_version": outcome.stream_version,
        "stream_offset": outcome.stream_offset,
        "status": status,
        "route": outcome.route,
        "plan_request": outcome.plan_request,
    }
