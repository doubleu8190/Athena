"""Decide whether the current task needs long-term memory retrieval."""

from __future__ import annotations

from langgraph.graph.state import StateNode

from ..state import AgentState
from ..services.memory_service import MemoryService


async def decide_memory(
    state: AgentState, *, memory_service: MemoryService
) -> AgentState:
    if not state.get("session_id"):
        raise ValueError("AgentState missing required field: session_id")
    request = await memory_service.build_memory_request(
        state.get("session_id", ""),
        state.get("user_message", ""),
        state.get("history", []),
    )
    return {"memory_request": request.model_dump(mode="json") if request else None}


def create_decide_memory_node(
    memory_service: MemoryService,
) -> StateNode[AgentState, None]:
    async def node(state: AgentState) -> AgentState:
        return await decide_memory(state, memory_service=memory_service)

    return node
