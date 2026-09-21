"""汇聚附件处理结果并选择 Agent 或失败路径。"""

from __future__ import annotations

from typing import Literal

from ..state import AgentState


def route_after_attachment_processing(
    state: AgentState,
) -> Literal["plan_context", "handle_attachment_failure"]:
    """附件完成后进入上下文计划；失败时结束当前请求。"""
    results = state.get("file_results", [])
    if all(item.get("status") == "ready" for item in results):
        return "plan_context"
    return "handle_attachment_failure"


def route_after_task_understanding(
    state: AgentState,
) -> Literal["clarification_response", "process_attachments", "plan_context"]:
    """根据任务理解结果决定澄清、按需处理附件或直接规划上下文。"""
    if state.get("clarification_question"):
        return "clarification_response"
    task_spec = state.get("task_spec") or {}
    requirements = task_spec.get("context_requirements", [])
    if "file" in requirements and state.get("requested_attachment_refs"):
        return "process_attachments"
    return "plan_context"
