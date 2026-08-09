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
from athena.models.tool import ToolResult
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


# ---------------------------------------------------------------------------
# 回归测试：stop_signal 接线 / LLM 流式超时 / 工具失败状态落库
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harness_stop_signal_interrupts_run(db: Database):
    """stop_signal 置位后 run 应立即终止，且 session 状态为 interrupted."""
    await db.create_session("s-stop")
    stop_signal = asyncio.Event()
    stop_signal.set()
    model = _StreamingTextModel()
    harness = Harness(
        llm=LLMProvider(model),
        tool_manager=UnifiedToolManager(),
        db=db,
        harness_settings=HarnessSettings(max_turns_per_run=5, retry_budget=2, tool_timeout=5),
    )
    result = await harness.run(
        messages=[{"role": "user", "content": "hi"}],
        session_id="s-stop",
        run_id="20260808",
        stop_signal=stop_signal,
    )
    assert result.interrupted is True
    session = await db.get_session("s-stop")
    assert session.status.value == "interrupted"


class _StopMidStreamModel:
    """yield 一个 chunk 后置位 stop_signal，再 yield 一个 chunk 让循环命中 stop 检查."""

    def __init__(self, stop_signal: asyncio.Event) -> None:
        self._stop_signal = stop_signal

    async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        return AIMessage(content="ok")

    def astream(self, messages: Any, **kwargs: Any) -> AsyncIterator[Any]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[Any]:
        yield AIMessageChunk(content="partial")
        self._stop_signal.set()
        yield AIMessageChunk(content=" more")
        await asyncio.sleep(3600)  # 不会到达：stop break 应在第二次 yield 后触发

    def bind_tools(self, tools: Any) -> "_StopMidStreamModel":
        return self

    def with_structured_output(self, schema: Any) -> Any:
        return self


@pytest.mark.asyncio
async def test_harness_stop_mid_stream_finalizes_step(db: Database):
    """流式中途 stop：llm step 应被终态化（非 running）、补发 LLM_CALL_END，
    且不落库残缺 assistant 消息."""
    await db.create_session("s-stopmid")
    stop_signal = asyncio.Event()
    ws = _RecordingWs()
    harness = Harness(
        llm=LLMProvider(_StopMidStreamModel(stop_signal)),
        tool_manager=UnifiedToolManager(),
        db=db,
        ws_manager=ws,
        harness_settings=HarnessSettings(
            max_turns_per_run=5, retry_budget=2, tool_timeout=5, llm_stream_timeout=30
        ),
    )
    result = await harness.run(
        messages=[{"role": "user", "content": "hi"}],
        session_id="s-stopmid",
        run_id="20260808",
        stop_signal=stop_signal,
    )
    assert result.interrupted is True
    # 当前 llm step 被终态化，无遗留 running
    steps = await db.get_steps("s-stopmid")
    assert steps
    assert all(s.status.value != "running" for s in steps)
    assert any(s.status.value == "failed" for s in steps)
    # 补发 LLM_CALL_END failed，前端气泡结束
    call_ends = [e for e in ws.events if e["type"] == EventType.LLM_CALL_END]
    assert call_ends and call_ends[-1]["data"]["status"] == "failed"
    # 不落库残缺 assistant 消息
    msgs = await db.get_messages("s-stopmid")
    assert all(m.role.value != "assistant" for m in msgs)
    session = await db.get_session("s-stopmid")
    assert session.status.value == "interrupted"


class _HangingIterator:
    """astream 挂死：__anext__ 永不返回."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)
        raise StopAsyncIteration


class _HangingModel:
    """astream 永不返回，用于验证 llm_stream_timeout 兜底."""

    async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        await asyncio.sleep(3600)

    def astream(self, messages: Any, **kwargs: Any) -> AsyncIterator[Any]:
        return _HangingIterator()

    def bind_tools(self, tools: Any) -> "_HangingModel":
        return self

    def with_structured_output(self, schema: Any) -> Any:
        return self


@pytest.mark.asyncio
async def test_harness_llm_stream_timeout_terminates_run(db: Database):
    """流式挂死时 llm_stream_timeout 应终态化 step，run 结束而非永久 running."""
    await db.create_session("s-hang")
    model = _HangingModel()
    harness = Harness(
        llm=LLMProvider(model),
        tool_manager=UnifiedToolManager(),
        db=db,
        harness_settings=HarnessSettings(
            max_turns_per_run=5, retry_budget=1, tool_timeout=5, llm_stream_timeout=1
        ),
    )
    result = await harness.run(
        messages=[{"role": "user", "content": "hi"}],
        session_id="s-hang",
        run_id="20260808",
    )
    assert result.error is not None
    steps = await db.get_steps("s-hang")
    assert steps
    # 所有 llm_call step 都被终态化（failed），无遗留 running
    assert all(s.status.value != "running" for s in steps)
    assert any(s.error_message and "超时" in s.error_message for s in steps)


def _tool_call_chunk() -> AIMessageChunk:
    """构造触发 exec_shell 工具调用的流式 chunk."""
    return AIMessageChunk(
        content="",
        tool_call_chunks=[
            {"index": 0, "id": "call_1", "name": "exec_shell", "args": '{"command": "boom"}'},
        ],
    )


class _ToolCallThenAnswerModel:
    """第一次 astream 返回工具调用，后续返回纯文本（避免无限循环）."""

    def __init__(self) -> None:
        self._calls = 0

    async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        return AIMessage(content="")

    def astream(self, messages: Any, **kwargs: Any) -> AsyncIterator[Any]:
        self._calls += 1
        if self._calls == 1:
            return _AsyncIterator([_tool_call_chunk()])
        return _AsyncIterator([AIMessage(content="final answer")])

    def bind_tools(self, tools: Any) -> "_ToolCallThenAnswerModel":
        return self

    def with_structured_output(self, schema: Any) -> Any:
        return self


@pytest.mark.asyncio
async def test_tool_failure_records_failure_not_success():
    """工具返回 failed 时熔断器应记失败而非 success（修复 record_result 顺序）."""
    harness = Harness(
        llm=LLMProvider(_FakeModel()),
        tool_manager=UnifiedToolManager(),
        harness_settings=HarnessSettings(max_turns_per_run=3, retry_budget=1, tool_timeout=5),
    )

    async def _fake_call_tool(**kwargs: Any) -> ToolResult:
        return ToolResult(status="failed", error="exit_code=2: boom")

    harness._tool_manager.call_tool = _fake_call_tool  # type: ignore[method-assign]

    content, status, err, stack = await harness._execute_single_tool(
        tool_name="exec_shell",
        args={"command": "boom"},
        session_id="s-rec",
        run_id="20260808",
        tool_call_id="tc-1",
    )
    assert status == "failed"
    assert err == "exit_code=2: boom"
    records = harness._error_handler.circuit_breaker._records["exec_shell"]
    assert records
    # 修复前会先无条件记一条 success，修复后全部为失败
    assert all(not ok for ok, _ in records)


@pytest.mark.asyncio
async def test_tool_failure_persisted_as_failed(db: Database):
    """完整 run：工具失败应落库 tool_call.status=failed，且失败详情回传给 LLM."""
    await db.create_session("s-toolfail")
    harness = Harness(
        llm=LLMProvider(_ToolCallThenAnswerModel()),
        tool_manager=UnifiedToolManager(),
        db=db,
        harness_settings=HarnessSettings(max_turns_per_run=3, retry_budget=1, tool_timeout=5),
    )

    async def _fake_call_tool(**kwargs: Any) -> ToolResult:
        return ToolResult(status="failed", error="exit_code=2: command failed")

    harness._tool_manager.call_tool = _fake_call_tool  # type: ignore[method-assign]

    result = await harness.run(
        messages=[{"role": "user", "content": "hi"}],
        session_id="s-toolfail",
        run_id="20260808",
    )
    assert result.error is None
    # tool_call 落库 failed
    tool_calls = await db.query_tool_calls("s-toolfail")
    assert tool_calls
    assert all(tc.status.value == "failed" for tc in tool_calls)
    # 失败详情进入 tool 消息，供 LLM 下一轮自愈
    tool_msgs = [m for m in await db.get_messages("s-toolfail") if m.role.value == "tool"]
    assert tool_msgs
    assert "exit_code=2: command failed" in tool_msgs[0].content
    # 工具 step 落库 failed
    tool_steps = [s for s in await db.get_steps("s-toolfail") if s.step_type.value == "tool_execution"]
    assert tool_steps
    assert all(s.status.value == "failed" for s in tool_steps)


@pytest.mark.asyncio
async def test_steps_record_parent_run_id(db: Database):
    """主 run 步骤 parent_run_id 为 None；子 run（带父链）步骤指向父 run."""
    await db.create_session("s-parent")
    harness = Harness(
        llm=LLMProvider(_StreamingTextModel()),
        tool_manager=UnifiedToolManager(),
        db=db,
        harness_settings=HarnessSettings(max_turns_per_run=5, retry_budget=2, tool_timeout=5),
    )
    # 主 run：不传 parent_run_id
    await harness.run(
        messages=[{"role": "user", "content": "main"}],
        session_id="s-parent",
        run_id="main_run",
    )
    main_steps = await db.get_steps("s-parent")
    assert main_steps
    assert all(s.parent_run_id is None for s in main_steps)

    # 子 run：同一 harness 实例传入 parent_run_id
    await harness.run(
        messages=[{"role": "user", "content": "sub"}],
        session_id="s-parent",
        run_id="main_run_1",
        parent_run_id="main_run",
    )
    sub_steps = [s for s in await db.get_steps("s-parent") if s.run_id == "main_run_1"]
    assert sub_steps
    assert all(s.parent_run_id == "main_run" for s in sub_steps)
