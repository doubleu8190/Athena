"""Harness 引擎回归测试 — 验证错误恢复、预算控制与空响应处理."""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any, AsyncIterator

import pytest

from langchain_core.messages import AIMessage, AIMessageChunk

from athena.core.harness.harness import Harness, HarnessSettings
from athena.core.llm.provider import LLMProvider
from athena.core.tools.manager import UnifiedToolManager
from athena.db.database import Database
from athena.schemas.events import EventType


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


class _EmptyThenAnswerModel:
    """第一次流式返回空响应，后续返回正常内容（模拟上游偶发空流）."""

    def __init__(self) -> None:
        self._calls = 0

    async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        return AIMessage(content="ok")

    def astream(self, messages: Any, **kwargs: Any) -> AsyncIterator[Any]:
        self._calls += 1
        if self._calls == 1:
            return _AsyncIterator([AIMessage(content="")])
        return _AsyncIterator([AIMessage(content="final answer")])

    def bind_tools(self, tools: Any) -> "_EmptyThenAnswerModel":
        return self

    def with_structured_output(self, schema: Any) -> Any:
        return self


class _AlwaysEmptyModel:
    """每次都返回空响应."""

    async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        return AIMessage(content="")

    def astream(self, messages: Any, **kwargs: Any) -> AsyncIterator[Any]:
        return _AsyncIterator([AIMessage(content="")])

    def bind_tools(self, tools: Any) -> "_AlwaysEmptyModel":
        return self

    def with_structured_output(self, schema: Any) -> Any:
        return self


class _RecordingWs:
    """记录所有推送事件的假 WS 管理器."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_to_session(self, session_id: str, message: dict[str, Any]) -> None:
        self.events.append(message)


class _StreamingTextModel:
    """流式返回文本分片（AIMessageChunk）."""

    async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        return AIMessage(content="ok")

    def astream(self, messages: Any, **kwargs: Any) -> AsyncIterator[Any]:
        return _AsyncIterator([
            AIMessageChunk(content="Hel"),
            AIMessageChunk(content="lo"),
        ])

    def bind_tools(self, tools: Any) -> "_StreamingTextModel":
        return self

    def with_structured_output(self, schema: Any) -> Any:
        return self


@pytest.fixture
async def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    await database.connect()
    yield database
    await database.close()
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.mark.asyncio
async def test_harness_retries_empty_response():
    """空响应应触发有界重试，而非静默保存空消息."""
    model = _EmptyThenAnswerModel()
    llm = LLMProvider(model)
    harness = Harness(
        llm=llm,
        tool_manager=UnifiedToolManager(),
        harness_settings=HarnessSettings(max_turns_per_run=5, retry_budget=2, tool_timeout=5),
    )
    result = await harness.run(
        messages=[{"role": "user", "content": "hi"}],
        session_id="s-empty-retry",
        run_id="20260730",
    )
    assert result.error is None
    assert result.content == "final answer"
    assert model._calls == 2  # 1 次空 + 1 次重试成功


@pytest.mark.asyncio
async def test_harness_empty_response_exhausted_returns_error():
    """重试耗尽后应返回可见错误，而非当作成功的空回答."""
    model = _AlwaysEmptyModel()
    llm = LLMProvider(model)
    harness = Harness(
        llm=llm,
        tool_manager=UnifiedToolManager(),
        harness_settings=HarnessSettings(max_turns_per_run=10, retry_budget=2, tool_timeout=5),
    )
    result = await harness.run(
        messages=[{"role": "user", "content": "hi"}],
        session_id="s-empty",
        run_id="20260730",
    )
    assert result.error is not None
    assert "空响应" in result.error


@pytest.mark.asyncio
async def test_harness_streams_text_content():
    """流式文本分片应被正确合并为完整 content."""
    model = _StreamingTextModel()
    llm = LLMProvider(model)
    harness = Harness(
        llm=llm,
        tool_manager=UnifiedToolManager(),
        harness_settings=HarnessSettings(max_turns_per_run=5, retry_budget=2, tool_timeout=5),
    )
    result = await harness.run(
        messages=[{"role": "user", "content": "hi"}],
        session_id="s-stream",
        run_id="20260730",
    )
    assert result.error is None
    assert result.content == "Hello"


def test_assemble_response_merges_tool_calls(harness: Harness):
    """流式 tool_call 分片应被正确合并为完整 tool_calls."""
    chunks = [
        AIMessageChunk(
            content="",
            tool_call_chunks=[{"index": 0, "id": "call_1", "name": "read_file", "args": '{"path":"a'}],
        ),
        AIMessageChunk(
            content="",
            tool_call_chunks=[{"index": 0, "args": 'b.txt"}'}],
        ),
    ]
    content, tcs = harness._assemble_response(chunks, "")
    assert content == ""
    assert tcs == [{"id": "call_1", "name": "read_file", "args": {"path": "ab.txt"}}]


@pytest.mark.asyncio
async def test_empty_response_does_not_save_message(db: Database):
    """空响应不应向数据库落库任何空 assistant 消息."""
    await db.create_session("s-empty-db")
    model = _AlwaysEmptyModel()
    llm = LLMProvider(model)
    harness = Harness(
        llm=llm,
        tool_manager=UnifiedToolManager(),
        db=db,
        harness_settings=HarnessSettings(max_turns_per_run=10, retry_budget=2, tool_timeout=5),
    )
    result = await harness.run(
        messages=[{"role": "user", "content": "hi"}],
        session_id="s-empty-db",
        run_id="20260730",
    )
    assert result.error is not None
    assert await db.get_messages("s-empty-db") == []


@pytest.mark.asyncio
async def test_harness_emits_llm_call_end_failed_on_empty_response():
    """空响应重试路径应补发 status=failed 的 LLM_CALL_END，供前端清理空气泡."""
    model = _EmptyThenAnswerModel()
    ws = _RecordingWs()
    harness = Harness(
        llm=LLMProvider(model),
        tool_manager=UnifiedToolManager(),
        ws_manager=ws,
        harness_settings=HarnessSettings(max_turns_per_run=5, retry_budget=2, tool_timeout=5),
    )
    result = await harness.run(
        messages=[{"role": "user", "content": "hi"}],
        session_id="s-end-empty",
        run_id="20260730",
    )
    assert result.error is None
    call_ends = [e for e in ws.events if e["type"] == EventType.LLM_CALL_END]
    assert len(call_ends) == 2  # 1 次空响应失败 + 1 次重试成功
    assert call_ends[0]["data"]["status"] == "failed"
    assert call_ends[1]["data"]["status"] == "completed"


@pytest.mark.asyncio
async def test_harness_emits_llm_call_end_failed_on_exception():
    """LLM 异常重试路径应补发 status=failed 的 LLM_CALL_END."""
    model = _FakeModel()  # 第一次 astream 抛异常
    ws = _RecordingWs()
    harness = Harness(
        llm=LLMProvider(model),
        tool_manager=UnifiedToolManager(),
        ws_manager=ws,
        harness_settings=HarnessSettings(max_turns_per_run=5, retry_budget=2, tool_timeout=5),
    )
    result = await harness.run(
        messages=[{"role": "user", "content": "hi"}],
        session_id="s-end-exc",
        run_id="20260730",
    )
    assert result.error is None
    call_ends = [e for e in ws.events if e["type"] == EventType.LLM_CALL_END]
    assert len(call_ends) == 2
    assert call_ends[0]["data"]["status"] == "failed"
    assert call_ends[1]["data"]["status"] == "completed"
