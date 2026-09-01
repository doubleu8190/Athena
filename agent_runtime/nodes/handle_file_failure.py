"""统一 Graph 的附件失败终点。"""

from __future__ import annotations

from ..state import AgentState


async def handle_file_failure(state: AgentState) -> AgentState:
    """返回文件错误结果；此节点不调用 Harness 或 LLM。"""
    failed = [
        item for item in state.get("file_results", []) if item.get("status") != "ready"
    ]
    details = "; ".join(
        f"{item.get('attachment_id')}: {item.get('error') or '处理失败'}"
        for item in failed
    )
    return {
        "result": {
            "content": f"附件处理失败，无法执行本次请求。{details}",
            "run_id": state["run_id"],
            "turn_count": 0,
            "tool_results": [],
            "error": details or "附件处理失败",
            "interrupted": False,
            "waiting": False,
            "file_results": state.get("file_results", []),
        },
        "error": details or "附件处理失败",
    }


def create_handle_file_failure_node():
    return handle_file_failure
