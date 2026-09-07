"""准备 Harness 执行上下文的 LangGraph 节点。"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from langgraph.graph.state import StateNode

from athena.models import Message

from ..state import AgentState

if TYPE_CHECKING:
    from ..graph_runtime import LangGraphRuntime


async def prepare_context(
    state: AgentState, *, runtime: LangGraphRuntime
) -> AgentState:
    """组合历史、附件和记忆，准备 Harness 输入。

    参数：
        state (AgentState): 必须对应已完成请求准备和记忆检索的状态。
        runtime (LangGraphRuntime): 当前图实例的运行时依赖。

    返回值：
        AgentState: 包含可检查点化的运行准备结果。

    异常：
        KeyError: 状态缺少历史消息或请求字段时抛出。
        运行时异常: Harness 输入准备失败时传播底层异常。
    """

    runtime.validate_state(state)
    session_id = state.get("session_id", "")
    run_id = state.get("run_id", "")
    message_id = state.get("message_id", "")
    message_content = state.get("user_message", "")
    history = [Message.model_validate(item) for item in state.get("history", [])]
    attachment_refs, harness_messages = await runtime.prepare_run(
        session_id,
        message_content,
        state.get("attachment_ids", []),
        history,
        run_id=run_id,
        message_id=message_id,
    )
    return {
        "attachment_refs": [item.model_dump(mode="json") for item in attachment_refs],
        "harness_messages": [item.model_dump(mode="json") for item in harness_messages],
    }


def create_prepare_context_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    """创建绑定指定运行时的 Harness 上下文准备节点。

    参数：
        runtime (LangGraphRuntime): 要注入节点的运行时依赖。

    返回值：
        StateNode[AgentState, None]: 可注册到 LangGraph 的异步节点。

    异常：
        不主动抛出异常；节点执行时的异常由 ``prepare_context`` 传播。
    """
    return partial(prepare_context, runtime=runtime)
