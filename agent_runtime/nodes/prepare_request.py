"""准备并持久化 Agent 请求的 LangGraph 节点。"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from langgraph.graph.state import StateNode

from ..state import AgentState

if TYPE_CHECKING:
    from ..graph_runtime import LangGraphRuntime


async def prepare_and_persist_request(
    state: AgentState, *, runtime: LangGraphRuntime
) -> AgentState:
    """规范化请求、加载上下文并持久化用户消息及附件关系。

    参数：
        state (AgentState): 至少包含非空 ``session_id``；可选提供消息、附件和续接数据。
        runtime (LangGraphRuntime): 当前图实例的运行时依赖。

    返回值：
        AgentState: 包含规范化请求数据和持久化消息 ID 的状态。

    异常：
        KeyError: ``state`` 缺少 ``session_id`` 时抛出。
        运行时异常: 附件或历史加载失败时传播底层运行时异常。
    """
    message = state.get("user_message", "")
    attachment_ids = list(dict.fromkeys(state.get("attachment_ids", []) or []))

    requested_attachments = await runtime.load_banded_attachments(
        state["session_id"], attachment_ids
    )
    history = await runtime.load_history(state["session_id"])
    prepared_state: AgentState = {
        "session_id": state["session_id"],
        "run_id": state["run_id"],
        "user_message": message,
        "attachment_ids": attachment_ids,
        "message_id": str(state.get("message_id") or ""),
        "history": [item.model_dump(mode="json") for item in history],
        "requested_attachment_refs": [
            item.to_ref().model_dump(mode="json") for item in requested_attachments
        ],
    }
    persisted_state = await runtime.persist_message_and_attachments(prepared_state)
    return {**prepared_state, **persisted_state}


def create_prepare_and_persist_request_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    """创建绑定指定运行时的请求准备和持久化节点。

    参数：
        runtime (LangGraphRuntime): 要注入节点的运行时依赖。

    返回值：
        StateNode[AgentState, None]: 可注册到 LangGraph 的异步节点。

    异常：
        不主动抛出异常；节点执行时的异常由 ``prepare_and_persist_request`` 传播。
    """
    return partial(prepare_and_persist_request, runtime=runtime)
