"""检索 Agent 记忆的 LangGraph 节点。"""

from __future__ import annotations

from langgraph.graph.state import StateNode

from ..state import AgentState
from ..services.memory_service import MemoryService


async def retrieve_memory(
    state: AgentState, *, memory_service: MemoryService
) -> AgentState:
    """为当前请求检索记忆上下文。

    参数：
        state: 必须包含 ``session_id`` 和 ``memory_request`` 字段。
        memory_service: 提供记忆检索能力的服务。

    返回值：
        AgentState: 仅包含新增的 ``memory_context`` 字段。
    """
    if not state.get("session_id"):
        raise ValueError("AgentState missing required field: session_id")
    memory_context = await memory_service.retrieve_memory_context(
        state.get("session_id", ""),
        state.get("memory_request"),
        run_id=state.get("run_id", ""),
    )
    return {"memory_context": memory_context}


def create_retrieve_memory_node(
    memory_service: MemoryService,
) -> StateNode[AgentState, None]:
    """创建绑定指定 MemoryService 的记忆检索节点。"""
    async def node(state: AgentState) -> AgentState:
        return await retrieve_memory(state, memory_service=memory_service)

    return node
