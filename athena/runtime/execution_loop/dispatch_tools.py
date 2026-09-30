"""用 LangGraph Send 将工具调用 fan-out 到单工具节点。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langgraph.types import Send

from ..state import AgentExecutionState

if TYPE_CHECKING:
    from ..langgraph_runtime import LangGraphRuntime


def dispatch_tool_calls(state: dict, *, graph_runtime: LangGraphRuntime):
    execution: AgentExecutionState = state.get("execution", {})
    calls = list(execution.get("recoverable", {}).get("pending_tool_calls", []))
    return [
        Send(
            "tool_call",
            {
                "execution": execution,
                "tool_call": call,
                "request": state.get("request", {}),
            },
        )
        for call in calls
    ]
