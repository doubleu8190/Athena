"""执行收尾节点。

关闭回答流、发布终态事件并打包 execution-loop 结果。
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
    session_id: str,
    run_id: str,
    message_id: str | None,
    error: str | None,
    error_detail: dict[str, object] | None,
) -> AgentExecutionState:
    """关闭流、发布终态事件并标记执行完成。"""
    executor = _executor(graph_runtime)
    recoverable = state.get("recoverable", {})
    derived = state.get("derived", {})
    await executor.finish_execution(
        session_id=session_id,
        run_id=run_id,
        message_id=message_id,
        stop_signal=_stop_signal(config),
        stream_version=int(recoverable.get("stream_version", 0)),
        stream_offset=int(recoverable.get("stream_offset", 0)),
        stream_started=bool(recoverable.get("stream_started", False)),
        turn_count=int(recoverable.get("turn_count", 0)),
        error=error,
        error_detail=error_detail,
        interrupted=bool(derived.get("interrupted", False)),
        parent_run_id=recoverable.get("parent_run_id"),
        content=recoverable.get("final_content", recoverable.get("last_content", "")),
    )
    return {"derived": {"status": "completed" if not error else "failed"}}
