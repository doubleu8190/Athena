"""持久化统一消息 Run 的用户消息和附件关系。"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from langgraph.graph.state import StateNode

from ..state import AgentState

if TYPE_CHECKING:
    from ..graph_runtime import LangGraphRuntime


async def persist_message_and_attachments(
    state: AgentState, *, runtime: LangGraphRuntime
) -> AgentState:
    """幂等创建用户消息，并在事务中绑定本次提交的附件。"""
    return await runtime.persist_message_and_attachments(state)


def create_persist_message_and_attachments_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    return partial(persist_message_and_attachments, runtime=runtime)
