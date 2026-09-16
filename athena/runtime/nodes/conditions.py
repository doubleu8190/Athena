"""汇聚附件处理结果并选择 Agent 或失败路径。"""

from __future__ import annotations

from typing import Literal

from ..state import AgentState


def route_after_attachment_processing(
    state: AgentState,
) -> Literal["build_memory_request", "handle_attachment_failure"]:
    """只有所有附件 READY 时才允许进入记忆检索。"""
    results = state.get("file_results", [])
    if all(item.get("status") == "ready" for item in results):
        return "build_memory_request"
    return "handle_attachment_failure"

def route_after_memory_request(
    state: AgentState,
) -> Literal["retrieve_memory", "prepare_harness_input"]:
    request = state.get("memory_request")
    # 检索是低风险的只读步骤：所有已经通过低价值/执行指令过滤的请求
    # 都允许进入检索，避免依赖固定 reason 白名单漏掉新类型的知识问题。
    return "retrieve_memory" if request else "prepare_harness_input"
