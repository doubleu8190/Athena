"""完成 Agent 响应的 LangGraph 节点。"""

from __future__ import annotations

from langgraph.graph.state import StateNode

from ..state import AgentState


async def finalize_response(state: AgentState) -> AgentState:
    """完成后处理并将领域结果转换为 JSON 安全响应。

    参数：
        state (AgentState): 包含当前会话和运行标识的图状态。
    返回值：
        AgentState: 包含 JSON 安全 ``result`` 的状态。

    异常：
        运行时异常: 后处理或结果序列化失败时传播底层异常。
    """
    result = state.get("result")
    if result is None and state.get("error"):
        result = {
            "content": "",
            "run_id": state["run_id"],
            "turn_count": 0,
            "tool_results": [],
            "error": state["error"],
            "error_detail": state.get("error_detail"),
            "interrupted": False,
            "attachments": [],
        }
    return {
        "session_id": state["session_id"],
        "run_id": state["run_id"],
        "message_id": state.get("message_id"),
        "result": result,
    }


def create_finalize_response_node() -> StateNode[AgentState, None]:
    """创建响应收尾节点。

    返回值：
        StateNode[AgentState, None]: 可注册到 LangGraph 的异步节点。

    异常：
        不主动抛出异常；节点执行时的异常由 ``finalize_response`` 传播。
    """
    return finalize_response
