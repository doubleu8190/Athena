"""单个工具调用节点。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..state import AgentState
from ._helpers import _stop_signal, _executor

if TYPE_CHECKING:
    from ..langgraph_runtime import LangGraphRuntime


async def tool_call(
    state: AgentState,
    config,
    *,
    graph_runtime: LangGraphRuntime,
    session_id: str,
    run_id: str,
    message_id: str | None,
) -> AgentState:
    execution = state.get("execution", {})
    call = state.get("tool_call", {})
    recoverable = execution.get("recoverable", {})
    harness = _executor(graph_runtime)
    outcome = await harness.execute_tool_call(
        tool_calls=[call],
        tool_results=list(recoverable.get("tool_results", [])),
        session_id=session_id,
        run_id=run_id,
        message_id=message_id,
        turn_count=int(recoverable.get("turn_count", 0)),
        stop_signal=_stop_signal(config),
        parent_run_id=recoverable.get("parent_run_id"),
        tool_names=recoverable.get("tool_names"),
        approval_decisions=(
            {
                str(call.get("id")): recoverable.get("approval_decisions", {}).get(
                    recoverable.get("approval_ids", {}).get(call.get("id"), "")
                )
            }
            if recoverable.get("approval_ids", {}).get(call.get("id"))
            else {}
        ),
    )
    result = next(
        (item for item in outcome.tool_results if item.get("tool_call_id") == call.get("id")),
        None,
    )
    return {
        "execution": {
            "recoverable": {
                "tool_result_deltas": [result] if result else [],
                "tool_message_deltas": outcome.messages,
            },
            "derived": {"interrupted": outcome.interrupted},
        }
    }
