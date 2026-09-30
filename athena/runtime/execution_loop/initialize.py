"""初始化 Agent 执行循环状态。"""

from __future__ import annotations

from ..state import AgentExecutionState, derive_execution_state, split_execution_state, AgentExecutionRecoverableState


def initialize_execution(state: AgentExecutionState) -> AgentExecutionState:
    """初始化执行状态；不执行外部副作用。"""

    recoverable, derived = split_execution_state(state)
    initialized_recoverable: AgentExecutionRecoverableState = {
        "system_prompt": recoverable.get("system_prompt", ""),
        "parent_run_id": recoverable.get("parent_run_id"),
        "tool_names": recoverable.get("tool_names"),
        "messages": list(recoverable.get("messages", [])),
        "pending_tool_calls": list(recoverable.get("pending_tool_calls", [])),
        "tool_results": list(recoverable.get("tool_results", [])),
        "tool_result_deltas": [],
        "tool_message_deltas": [],
        "approval_batch": recoverable.get("approval_batch"),
        "approval_decisions": dict(recoverable.get("approval_decisions", {})),
        "approval_ids": dict(recoverable.get("approval_ids", {})),
        "turn_count": int(recoverable.get("turn_count", 0)),
        "retry_count": int(recoverable.get("retry_count", 0)),
        "max_turns": int(recoverable.get("max_turns", 20)),
        "max_retries": int(recoverable.get("max_retries", 3)),
        "last_content": recoverable.get("last_content", ""),
        "final_content": recoverable.get("final_content", ""),
        "stream_started": bool(recoverable.get("stream_started", False)),
        "stream_version": int(recoverable.get("stream_version", 0)),
        "stream_offset": int(recoverable.get("stream_offset", 0)),
    }
    return {
        "recoverable": initialized_recoverable,
        "derived": derive_execution_state(initialized_recoverable, derived),
    }
