"""完成 Agent 响应的 LangGraph 节点。"""

from __future__ import annotations

from langgraph.graph.state import StateNode

from ..state import AgentState


async def assemble_final_response(state: AgentState) -> AgentState:
    """完成后处理并将领域结果转换为 JSON 安全响应。

    参数：
        state (AgentState): 包含当前会话和运行标识的图状态。
    返回值：
        AgentState: 包含 JSON 安全 ``result`` 的状态。

    异常：
        运行时异常: 后处理或结果序列化失败时传播底层异常。
    """
    request = state.get("request", {})
    response = state.get("response", {})
    result = response.get("result")
    if result is None and response.get("error"):
        result = {
            "content": "",
            "run_id": request["run_id"],
            "turn_count": 0,
            "tool_results": [],
            "error": response["error"],
            "error_detail": response.get("error_detail"),
            "interrupted": False,
            "attachments": [],
        }
    return {
        "phase": "completed",
        "response": {
            **response,
            "result": result,
        },
    }


def create_assemble_final_response_node() -> StateNode[AgentState, None]:
    """创建响应收尾节点。

    返回值：
        StateNode[AgentState, None]: 可注册到 LangGraph 的异步节点。

    异常：
        不主动抛出异常；节点执行时的异常由 ``assemble_final_response`` 传播。
    """
    return assemble_final_response
