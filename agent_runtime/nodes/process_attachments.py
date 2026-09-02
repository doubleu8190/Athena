"""在统一 Agent Run 内并行处理本次消息携带的附件。"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING

from langgraph.graph.state import StateNode

from ..state import AgentState

if TYPE_CHECKING:
    from ..graph_runtime import LangGraphRuntime


async def process_attachments(
    state: AgentState, *, runtime: LangGraphRuntime
) -> AgentState:
    """处理全部附件并将小型、可检查点化的结果写入 State。"""
    message_id = state.get("message_id") or state.get("user_message_id")
    if not message_id:
        raise KeyError("message_id")
    results = await asyncio.gather(
        *(
            runtime.process_attachment(
                attachment_id,
                message_id,
                state["session_id"],
                state["run_id"],
            )
            for attachment_id in state.get("attachment_ids", [])
        )
    )
    return {
        "session_id": state["session_id"],
        "run_id": state["run_id"],
        "message_id": message_id,
        "file_results": results,
        "files_ready": all(item["status"] == "ready" for item in results),
    }


def create_process_attachments_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    return partial(process_attachments, runtime=runtime)
