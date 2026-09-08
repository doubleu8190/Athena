"""Decide whether the current task needs long-term memory retrieval."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Literal

from langgraph.graph.state import StateNode

from athena.core.memory.contracts import MemoryRetrievalRequest

from ..state import AgentState

if TYPE_CHECKING:
    from ..graph_runtime import LangGraphRuntime


async def decide_memory(state: AgentState, *, runtime: LangGraphRuntime) -> AgentState:
    runtime.validate_state(state)
    request = await runtime.build_memory_request(
        state.get("session_id", ""),
        state.get("user_message", ""),
        state.get("history", []),
    )
    return {"memory_request": request.model_dump(mode="json") if request else None}


def create_decide_memory_node(runtime: LangGraphRuntime) -> StateNode[AgentState, None]:
    return partial(decide_memory, runtime=runtime)
