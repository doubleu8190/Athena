"""汇聚附件处理结果并选择 Agent 或失败路径。"""

from __future__ import annotations

from typing import Literal

from ..state import AgentState


def route_after_attachment_processing(
    state: AgentState,
) -> Literal["understand_task", "handle_attachment_failure"]:
    """只有所有附件 READY 时才允许进入任务理解。"""
    results = state.get("file_results", [])
    if all(item.get("status") == "ready" for item in results):
        return "understand_task"
    return "handle_attachment_failure"


def route_after_task_understanding(
    state: AgentState,
) -> Literal["clarification_response", "plan_context"]:
    """目标不明确时先向用户澄清，否则继续获取上下文。"""
    if state.get("clarification_question"):
        return "clarification_response"
    return "plan_context"
