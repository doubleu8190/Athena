"""Harness 引擎回归测试 — 验证错误恢复与预算控制."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from langchain_core.messages import AIMessage

from athena.core.harness.harness import Harness, HarnessSettings
from athena.core.llm.provider import LLMProvider
from athena.core.tools.manager import UnifiedToolManager


class _AsyncIterator:
    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self) -> Any:
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item


class _FakeModel:
    """用于模拟 LLM Provider 的简单模型.

    第一次 astream 抛出 RuntimeError，后续调用正常返回 AIMessage。
    """

    def __init__(self) -> None:
        self._call_index = 0

    async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        return AIMessage(content="ok")

    def astream(self, messages: Any, **kwargs: Any) -> AsyncIterator[Any]:
        self._call_index += 1
        if self._call_index == 1:
            raise RuntimeError("invalid_request_error: tool x not found")

        return _AsyncIterator([AIMessage(content="final answer")])

    def bind_tools(self, tools: Any) -> "_FakeModel":
        return self

    def with_structured_output(self, schema: Any) -> Any:
        return self


class _FailAlwaysModel:
    """每次 astream 调用都抛出 RuntimeError."""

    async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        return AIMessage(content="ok")

    def astream(self, messages: Any, **kwargs: Any) -> AsyncIterator[Any]:
        raise RuntimeError("simulated persistent failure")

    def bind_tools(self, tools: Any) -> "_FailAlwaysModel":
        return self

    def with_structured_output(self, schema: Any) -> Any:
        return self


@pytest.fixture
def harness():
    model = _FakeModel()
    llm = LLMProvider(model)
    tool_manager = UnifiedToolManager()
    return Harness(
        llm=llm,
        tool_manager=tool_manager,
        harness_settings=HarnessSettings(max_turns_per_run=5, retry_budget=3, tool_timeout=5),
    )


@pytest.mark.asyncio
async def test_harness_recovers_from_llm_error(harness: Harness):
    result = await harness.run(
        messages=[{"role": "user", "content": "hi"}],
        session_id="s-test",
        run_id="20260730",
    )
    assert result.error is None
    assert result.run_id == "20260730"
    assert result.content == "final answer"
    assert result.turn_count >= 2  # 至少包含一次失败 + 一次成功


@pytest.mark.asyncio
async def test_harness_budget_exceeded_raises():
    model = _FailAlwaysModel()
    llm = LLMProvider(model)
    harness = Harness(
        llm=llm,
        tool_manager=UnifiedToolManager(),
        harness_settings=HarnessSettings(max_turns_per_run=10, retry_budget=1, tool_timeout=5),
    )
    result = await harness.run(
        messages=[{"role": "user", "content": "hi"}],
        session_id="s-budget",
        run_id="20260730",
    )
    assert result.error is not None
    assert "重试预算超限" in result.error
