"""并行工具批次执行节点。

将当前 LLM 响应中的待处理工具调用并行执行，结果回写为增量状态。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig

from ..state import AgentExecutionState
from ._helpers import _stop_signal, _executor

if TYPE_CHECKING:
    from ..langgraph_runtime import LangGraphRuntime


async def execute_tool_batch(
    state: AgentExecutionState,
    config: RunnableConfig,
    *,
    graph_runtime: LangGraphRuntime,
) -> AgentExecutionState:
    """并行执行当前 LLM 响应的工具调用批次。"""
    harness = _executor(graph_runtime)
    if state.get("interrupted"):
        return {"status": "interrupted"}
    outcome = await harness.execute_tool_batch(
        tool_calls=state.get("pending_tool_calls", []),
        tool_results=state.get("tool_results", []),
        session_id=state.get("session_id", ""),
        run_id=state.get("run_id", ""),
        message_id=state.get("message_id"),
        turn_count=int(state.get("turn_count", 0)),
        stop_signal=_stop_signal(config),
        parent_run_id=state.get("parent_run_id"),
        tool_names=state.get("tool_names"),
    )
    failed_count = sum(
        1
        for item in outcome.tool_results
        if item.get("status") not in {None, "success"}
    )
    error = None
    if failed_count >= int(state.get("max_retries", 3)):
        error = (
            f"工具调用失败次数达到上限: {failed_count}/{state.get('max_retries', 3)}"
        )
    # 工具节点返回的是消息增量；必须保留此前的用户消息和 assistant 工具调用，
    # 否则下一轮 LLM 只能看到孤立的 ToolMessage，无法理解当前请求和调用关系。
    messages = [*state.get("messages", []), *outcome.messages]
    return {
        "messages": messages,
        "tool_results": outcome.tool_results,
        "interrupted": outcome.interrupted,
        "error": error,
        "status": "interrupted" if outcome.interrupted else "running",
    }
