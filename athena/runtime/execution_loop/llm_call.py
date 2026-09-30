"""单次 LLM 调用节点。

接收 ``AgentExecutionState``，通过 ``TurnExecutor`` 执行一次
LLM 调用，将结果（内容、工具调用、错误等）回写为增量状态更新。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
    session_id: str,
    run_id: str,
) -> tuple[AgentExecutionState, str | None, dict[str, Any] | None, dict[str, object] | None]:
    """执行恰好一次 LLM 调用。"""
    recoverable = state.get("recoverable", {})
    derived = state.get("derived", {})
    harness = _executor(graph_runtime)
    outcome = await harness.run_llm_turn(
        messages=recoverable.get("messages", []),
        session_id=session_id,
        run_id=run_id,
        system_prompt=recoverable.get("system_prompt", ""),
        turn_count=int(recoverable.get("turn_count", 0)),
        retry_count=int(recoverable.get("retry_count", 0)),
        max_turns=int(recoverable.get("max_turns", 20)),
        max_retries=int(recoverable.get("max_retries", 3)),
        stop_signal=_stop_signal(config),
        stream_started=bool(recoverable.get("stream_started", False)),
        stream_version=int(recoverable.get("stream_version", 0)),
        stream_offset=int(recoverable.get("stream_offset", 0)),
        parent_run_id=recoverable.get("parent_run_id"),
        tool_names=recoverable.get("tool_names"),
        retry_feedback=derived.get("retry_feedback"),
    )
    status = (
        "interrupted"
        if outcome.interrupted
        else ("failed" if outcome.error and not outcome.retryable else "running")
    )
    return {
        "recoverable": {
            "messages": outcome.messages,
            "pending_tool_calls": outcome.tool_calls,
            "turn_count": outcome.turn_count,
            "retry_count": outcome.retry_count,
            "last_content": outcome.content,
            "final_content": outcome.content or recoverable.get("final_content", ""),
            "stream_started": outcome.stream_started,
            "stream_version": outcome.stream_version,
            "stream_offset": outcome.stream_offset,
        },
        "derived": {
            "llm_result_status": outcome.llm_result_status,
            "llm_retry_reason": outcome.llm_retry_reason,
            "retry_feedback": outcome.retry_feedback,
            "retryable": outcome.retryable,
            "interrupted": outcome.interrupted,
            "status": status,
            "route": outcome.route,
        },
    }, outcome.error, outcome.error_detail, outcome.plan_request
