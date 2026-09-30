"""准备并持久化 Agent 请求的 LangGraph 节点。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langgraph.graph.state import StateNode

from ..state import AgentState

if TYPE_CHECKING:
    from ..services.session_context_service import SessionContextService


async def prepare_request_and_persist_message(
    state: AgentState, *, session_context_service: SessionContextService
) -> AgentState:
    """规范化请求、加载上下文并持久化用户消息及附件关系。

    参数：
        state (AgentState): 至少包含非空 ``session_id``；可选提供消息、附件和续接数据。
        session_context_service (SessionContextService): 会话上下文服务。

    返回值：
        AgentState: 包含规范化请求数据和持久化消息 ID 的状态。

    异常：
        KeyError: ``state`` 缺少 ``session_id`` 时抛出。
        运行时异常: 附件或历史加载失败时传播底层运行时异常。
    """
    request = state.get("request", {})
    if not request.get("session_id"):
        raise ValueError("AgentState missing required field: session_id")
    session_id = request["session_id"]
    # Attachment IDs are a set-like request field; preserve first-seen order
    # so retries produce the same durable message projection.
    attachment_ids = list(dict.fromkeys(request.get("attachment_ids", [])))
    attachments = await session_context_service.load_and_validate_attachments(
        session_id, attachment_ids
    )

    history = await session_context_service.load_history(session_id)
    prepared_state: AgentState = {
        "phase": "task_understanding",
        "request": {
            "session_id": session_id,
            "run_id": request.get("run_id", ""),
            "message_id": str(request.get("message_id") or ""),
            "user_message": request.get("user_message", ""),
            "attachment_ids": attachment_ids,
            "history": [item.model_dump(mode="json") for item in history],
            "requested_attachment_refs": [
                item.to_reference().model_dump(mode="json") for item in attachments
            ],
        },
    }
    await session_context_service.persist_message_and_attachments(
        prepared_state["request"]
    )
    return prepared_state


def create_prepare_request_and_persist_message_node(
    session_context_service: SessionContextService,
) -> StateNode[AgentState, None]:
    """创建绑定指定请求服务的请求准备和持久化节点。

    参数：
        session_context_service (SessionContextService): 要注入节点的会话上下文服务。

    返回值：
        StateNode[AgentState, None]: 可注册到 LangGraph 的异步节点。

    异常：
        不主动抛出异常；节点执行时的异常由 ``prepare_request_and_persist_message`` 传播。
    """
    async def node(state: AgentState) -> AgentState:
        return await prepare_request_and_persist_message(
            state, session_context_service=session_context_service
        )

    return node
