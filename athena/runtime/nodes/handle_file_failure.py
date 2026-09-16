"""统一 Graph 的附件失败终点。"""

from __future__ import annotations

from ..state import AgentState


async def handle_attachment_failure(state: AgentState) -> AgentState:
    """返回附件错误结果；此节点不调用 Harness 或 LLM。"""
    failed = [
        item for item in state.get("file_results", []) if item.get("status") != "ready"
    ]
    details = "; ".join(
        f"{item.get('attachment_id')}: {item.get('error') or '处理失败'}"
        for item in failed
    )
    return {
        "error": f"附件处理失败，无法执行本次请求。{details}",
    }


def create_handle_attachment_failure_node():
    return handle_attachment_failure
