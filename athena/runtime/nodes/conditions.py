"""LangGraph 主图的条件路由。"""

from __future__ import annotations

from typing import Literal

from ..state import AgentState


def route_after_task_understanding(
    state: AgentState,
) -> Literal["clarification_response", "plan_context"]:
    """根据任务理解结果决定澄清或规划上下文。"""
    if state.get("understanding", {}).get("clarification_question"):
        return "clarification_response"
    return "plan_context"
