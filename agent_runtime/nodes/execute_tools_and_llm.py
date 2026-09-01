"""执行附件等待、工具调用和 LLM 对话的 LangGraph 节点。"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import StateNode

from ..state import AgentState

if TYPE_CHECKING:
    from ..graph_runtime import LangGraphRuntime


async def execute_tools_and_llm(
    state: AgentState,
    config: RunnableConfig,
    *,
    runtime: LangGraphRuntime,
) -> AgentState:
    """执行附件等待、工具调用和 LLM 对话。

    参数：
        state (AgentState): 已完成 Harness 输入准备的图状态。
        config (RunnableConfig): LangGraph 配置，可能携带取消用的停止信号。
        runtime (LangGraphRuntime): 当前图实例的运行时依赖。

    返回值：
        AgentState: 包含可检查点化的最终结果。

    异常：
        KeyError: 状态缺少运行前准备字段，或配置结构不符合预期时抛出。
        运行时异常: 附件等待、工具调用或 LLM 执行失败时传播底层异常。
    """
    attachment_refs = runtime.deserialize_attachment_refs(
        state.get("attachment_refs", [])
    )
    messages = runtime.deserialize_messages(state["harness_messages"])
    stop_signal = config.get("configurable", {}).get("stop_signal")
    runtime.set_stop_signal(state["session_id"], stop_signal)
    try:
        result = await runtime.run_harness(
            messages,
            state["session_id"],
            state.get("system_prompt", ""),
            state["run_id"],
            stop_signal,
        )
        await runtime.post_process(state["session_id"], state["user_message"], result)
        return {"result": runtime.result_payload(result, attachment_refs)}
    finally:
        runtime.set_stop_signal(state["session_id"], None)


def create_execute_tools_and_llm_node(
    runtime: LangGraphRuntime,
) -> StateNode[AgentState, None]:
    """创建绑定指定运行时的工具和 LLM 执行节点。

    参数：
        runtime (LangGraphRuntime): 要注入节点的运行时依赖。

    返回值：
        StateNode[AgentState, None]: 可注册到 LangGraph 的异步节点。

    异常：
        不主动抛出异常；节点执行时的异常由 ``execute_tools_and_llm`` 传播。
    """
    return partial(execute_tools_and_llm, runtime=runtime)
