"""检索 Agent 记忆的 LangGraph 节点。"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from langgraph.graph.state import StateNode

from ..state import AgentState

if TYPE_CHECKING:
    from ..graph_runtime import LangGraphRuntime


async def retrieve_memory(
    state: AgentState, *, runtime: LangGraphRuntime
) -> AgentState:
    """为当前请求检索记忆上下文。

    参数：
        state (AgentState): 必须包含 ``session_id``，可选包含规范化后的用户消息。
        runtime (LangGraphRuntime): 当前图实例的运行时依赖。

    返回值：
        AgentState: 仅包含新增的 ``memory_context`` 字段。

    异常：
        KeyError: ``state`` 缺少 ``session_id``，或请求准备节点未建立对应上下文时抛出。
        运行时异常: 记忆检索失败时传播底层运行时异常。
    """
    runtime.validate_state(state)
    memory_context = await runtime.retrieve_memory_context(
        state.get("session_id", ""), state.get("memory_request")
    )
    return {
        "memory_context": memory_context,
    }


def create_retrieve_memory_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    """创建绑定指定运行时的记忆检索节点。

    参数：
        runtime (LangGraphRuntime): 要注入节点的运行时依赖。

    返回值：
        StateNode[AgentState, None]: 可注册到 LangGraph 的异步节点。

    异常：
        不主动抛出异常；节点执行时的异常由 ``retrieve_memory`` 传播。
    """
    return partial(retrieve_memory, runtime=runtime)
