"""汇聚附件处理结果并选择 Agent 或失败路径。"""

from __future__ import annotations

from typing import Literal

from ..state import AgentState


def check_file_results(state: AgentState) -> Literal["prepare_context", "handle_file_failure"]:
    """只有所有附件 READY 时才允许进入 Agent 上下文准备。"""
    results = state.get("file_results", [])
    if all(item.get("status") == "ready" for item in results):
        return "prepare_context"
    return "handle_file_failure"

