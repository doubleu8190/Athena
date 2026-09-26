"""Agent 执行循环节点共享工具。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig

if TYPE_CHECKING:
    from ..langgraph_runtime import LangGraphRuntime
    from athena.core.harness.turn_executor import HarnessTurnExecutor


def _stop_signal(config: RunnableConfig) -> asyncio.Event:
    """从 LangGraph 配置中提取停止信号。"""
    signal = config.get("configurable", {}).get("stop_signal")
    if signal is None:
        return asyncio.Event()
    if not isinstance(signal, asyncio.Event):
        raise TypeError("configurable.stop_signal must be an asyncio.Event")
    return signal


def _executor(runtime: LangGraphRuntime) -> HarnessTurnExecutor:
    """为当前节点创建轮次执行器。"""

    from athena.core.harness.turn_executor import HarnessTurnExecutor

    return HarnessTurnExecutor(
        llm=runtime._llm,
        tool_manager=runtime._tool_manager,
        settings=runtime._settings,
        db=runtime._db,
        compressor=runtime._compressor,
        event_publisher=runtime._event_publisher,
    )
