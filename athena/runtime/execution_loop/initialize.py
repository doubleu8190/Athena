"""初始化 Agent 执行状态的节点。

将主图状态映射为子图内部使用的扁平 ``AgentExecutionState``。
纯函数，无外部副作用，可直接写入 LangGraph checkpoint。
"""

from __future__ import annotations

from ..state import AgentExecutionState


def initialize_execution(state: AgentExecutionState) -> AgentExecutionState:
    """初始化执行状态；不执行外部副作用。"""

    return {
        "session_id": state.get("session_id", ""),
        "run_id": state.get("run_id", ""),
        "message_id": state.get("message_id", ""),
        "user_message": state.get("user_message", ""),
        "task_spec": state.get("task_spec"),
        "context_bundle": state.get("context_bundle"),
        "system_prompt": state.get("system_prompt", ""),
        "parent_run_id": state.get("parent_run_id"),
        "tool_names": state.get("tool_names"),
        "messages": list(state.get("messages", [])),
        "attachment_refs": list(state.get("attachment_refs", [])),
        "pending_tool_calls": list(state.get("pending_tool_calls", [])),
        "pending_approvals": list(state.get("pending_approvals", [])),
        "tool_results": list(state.get("tool_results", [])),
        "turn_count": int(state.get("turn_count", 0)),
        "retry_count": int(state.get("retry_count", 0)),
        "max_turns": int(state.get("max_turns", 20)),
        "max_retries": int(state.get("max_retries", 3)),
        "last_content": state.get("last_content", ""),
        "final_content": state.get("final_content", ""),
        "error": state.get("error"),
        "retryable": bool(state.get("retryable", False)),
        "interrupted": bool(state.get("interrupted", False)),
        "stream_started": bool(state.get("stream_started", False)),
        "stream_version": int(state.get("stream_version", 0)),
        "stream_offset": int(state.get("stream_offset", 0)),
        "status": "running",
        "route": state.get("route", "agent_loop"),
        "plan_request": state.get("plan_request"),
    }
