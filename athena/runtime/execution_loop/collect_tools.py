"""工具 fan-out 结果汇总节点。"""

from __future__ import annotations

from ..state import AgentExecutionState


async def collect_tool_results(state: AgentExecutionState, config) -> AgentExecutionState:
    recoverable = state.get("recoverable", {})
    calls = {
        str(call.get("id")): index
        for index, call in enumerate(recoverable.get("pending_tool_calls", []))
    }
    results = sorted(
        recoverable.get("tool_result_deltas", []),
        key=lambda item: calls.get(str(item.get("tool_call_id")), 10**9),
    )
    messages = sorted(
        recoverable.get("tool_message_deltas", []),
        key=lambda item: calls.get(str(item.get("tool_call_id")), 10**9),
    )
    return {
        "recoverable": {
            "messages": [*recoverable.get("messages", []), *messages],
            "tool_results": [*recoverable.get("tool_results", []), *results],
            "tool_result_deltas": [],
            "tool_message_deltas": [],
            "pending_tool_calls": [],
            "approval_batch": None,
            "approval_ids": {},
            "approval_decisions": {},
        },
        "derived": {"status": "running"},
    }
