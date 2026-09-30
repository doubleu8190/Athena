"""为独立 Worker 和子 Agent 执行可检查点化的 Agent loop。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from langgraph.graph.state import RunnableConfig

from .graph import build_agent_loop

if TYPE_CHECKING:
    from ..langgraph_runtime import LangGraphRuntime


async def run_agent_loop(
    runtime: LangGraphRuntime,
    *,
    session_id: str,
    run_id: str,
    user_message: str,
    system_prompt: str,
    tool_names: list[str],
    max_turns: int,
    stop_signal: asyncio.Event | None = None,
    parent_run_id: str | None = None,
    plan_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """运行一个独立的 Agent loop，并返回最终结果内容。

    参数:
        runtime: 持有 LLM、工具和事件依赖的 LangGraph 运行时。
        session_id: 所属会话标识。
        run_id: 当前独立运行标识，同时作为图线程标识。
        user_message: 子 Agent 或 Worker 的任务内容。
        system_prompt: 当前 Agent 的系统提示词。
        tool_names: 当前运行允许使用的工具名称。
        max_turns: 当前运行允许的最大 LLM 轮次。
        stop_signal: 可选的外部停止信号。
        parent_run_id: 父运行标识。
        plan_id: 所属计划标识。
        task_id: 所属任务标识。

    返回值:
        dict[str, Any]: execution-loop finish 节点生成的 JSON 结果。

    异常:
        RuntimeError: 图没有生成最终结果。
    """
    graph = build_agent_loop(runtime, checkpointer=getattr(runtime, "_checkpointer", None))
    signal = stop_signal or asyncio.Event()
    config: RunnableConfig = {
        "configurable": {"thread_id": run_id, "stop_signal": signal},
        "metadata": {"session_id": session_id, "run_id": run_id},
    }
    state = await graph.ainvoke(
        {
            "phase": "agent_loop",
            "request": {
                "session_id": session_id,
                "run_id": run_id,
                "user_message": user_message,
            },
            "execution": {
                "recoverable": {
                    "parent_run_id": parent_run_id,
                    "system_prompt": system_prompt,
                    "messages": [{"role": "user", "content": user_message}],
                    "tool_names": tool_names,
                    "max_turns": max_turns,
                    "max_retries": runtime._settings.retry_budget,
                },
            },
        },
        config=config,
    )
    result = state.get("response", {}).get("harness_result") if isinstance(state, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("Agent loop did not produce a final result")
    return result
