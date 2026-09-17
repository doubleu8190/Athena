"""执行收尾节点。

关闭回答流、发布终态事件并打包 Harness 结果。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig

from ..state import AgentExecutionState
from ._helpers import _stop_signal, _executor

if TYPE_CHECKING:
    from ..langgraph_runtime import LangGraphRuntime


async def finish_execution(
    state: AgentExecutionState,
    config: RunnableConfig,
    *,
    graph_runtime: LangGraphRuntime,
) -> AgentExecutionState:
    """关闭流、发布终态事件并标记执行完成。"""
    harness = _executor(graph_runtime)
    tool_results = state.get("tool_results", [])
    await harness.finish_execution(
        session_id=state.get("session_id", ""),
        run_id=state.get("run_id", ""),
        message_id=state.get("message_id"),
        stop_signal=_stop_signal(config),
        stream_version=int(state.get("stream_version", 0)),
        stream_offset=int(state.get("stream_offset", 0)),
        turn_count=int(state.get("turn_count", 0)),
        error=state.get("error"),
        error_detail=state.get("error_detail"),
        interrupted=bool(state.get("interrupted", False)),
        parent_run_id=state.get("parent_run_id"),
        content=state.get("final_content", state.get("last_content", "")),
    )
    return {
        "status": "completed" if not state.get("error") else "failed",
        "harness_result": {
            "content": state.get("final_content", state.get("last_content", "")),
            "run_id": state.get("run_id", ""),
            "turn_count": state.get("turn_count", 0),
            "tool_results": tool_results,
            "error": state.get("error"),
            "error_detail": state.get("error_detail"),
            "interrupted": state.get("interrupted", False),
        },
    }
