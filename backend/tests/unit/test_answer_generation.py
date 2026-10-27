"""正文首片段、连续输出刷新和失败清理的回归验证。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from application.orchestration import answer_generation as module
from application.orchestration.answer_generation import (
    AnswerGenerationError, AnswerGenerationInput, AnswerGenerationService, AnswerStreamingOptions,
)
from application.orchestration.root_decision import RootMessage
from domain.events import ApplicationEvent
from domain.integrations import LLMStreamEnd, LLMStreamEvent, LLMTextDelta
from tests.fakes.leases import lease_grant


class Events:
    """保留持久增量并提供首片段同步点。"""

    def __init__(self) -> None:
        """无参数，初始化事件列表和同步点，无业务异常。"""
        self.deltas: list[str] = []
        self.first_delta = asyncio.Event()

    async def publish(self, event: ApplicationEvent) -> ApplicationEvent:
        """参数 event 为待写事件；记录增量并返回原事件，不主动抛出异常。"""
        if event.event_type == "message.delta":
            self.deltas.append(str(event.payload["delta"]))
            self.first_delta.set()
        return event

    async def publish_with_lease(self, event: ApplicationEvent, *, lease) -> ApplicationEvent:
        return await self.publish(event)


def inputs() -> AnswerGenerationInput:
    """无参数，返回最小合法正文输入，不主动抛出异常。"""
    return AnswerGenerationInput(
        session_id="s", run_id="r", message_id="m", kind="final",
        execution_lease=lease_grant("root_execution", "r"),
        instructions="按资料回答", messages=(RootMessage(role="user", content="问题"),),
    )


@pytest.mark.asyncio
async def test_first_delta_is_persisted_before_next_provider_chunk() -> None:
    """首个短片段立即可见，不等待合并定时器或模型下一片段；无参数。"""
    release = asyncio.Event()
    events = Events()

    class LLM:
        def count_text_tokens(self, text: str) -> int:
            """参数 text 为模型输入，返回字符估算，无业务异常。"""
            return len(text)

        async def stream(self, messages: list) -> AsyncIterator[LLMStreamEvent]:
            """参数 messages 为输入，返回首片段后等待门闩，再正常结束。"""
            yield LLMTextDelta("先显示")
            await release.wait()
            yield LLMStreamEnd("stop")

    service = AnswerGenerationService(LLM(), events, prompt="回答", options=AnswerStreamingOptions(flush_interval_ms=10_000))
    pending = asyncio.create_task(service.generate(inputs()))
    try:
        await asyncio.wait_for(events.first_delta.wait(), timeout=0.2)
        assert events.deltas == ["先显示"]
        assert not pending.done()
    finally:
        release.set()
        await pending


@pytest.mark.asyncio
async def test_busy_stream_flushes_by_elapsed_time_without_waiting_for_a_pause(monkeypatch) -> None:
    """持续有片段到达时也按经过时间提交，无需真实睡眠或供应商停顿。"""
    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    events = Events()
    snapshots: list[list[str]] = []

    class LLM:
        def count_text_tokens(self, text: str) -> int:
            """参数 text 为模型输入，返回字符估算，无业务异常。"""
            return len(text)

        async def stream(self, messages: list) -> AsyncIterator[LLMStreamEvent]:
            """参数 messages 为输入；每次到达推进虚拟时间并观察已持久片段。"""
            for text in "abcde":
                snapshots.append(list(events.deltas))
                clock[0] += 0.04
                yield LLMTextDelta(text)
            yield LLMStreamEnd("stop")

    result = await AnswerGenerationService(LLM(), events, prompt="回答").generate(inputs())
    assert snapshots[1] == ["a"]
    assert snapshots[4] == ["a", "bcd"]
    assert result.content == "abcde" == "".join(events.deltas)
    assert result.answer_stream.last_chunk_id == len(events.deltas)


@pytest.mark.asyncio
async def test_iterator_cleanup_failure_preserves_answer_timeout_phase() -> None:
    """取消底层读取时的清理异常不能覆盖原来的正文超时；无参数。"""
    class LLM:
        def count_text_tokens(self, text: str) -> int:
            """参数 text 为模型输入，返回字符估算，无业务异常。"""
            return len(text)

        async def stream(self, messages: list) -> AsyncIterator[LLMStreamEvent]:
            """参数 messages 为输入；阻塞后模拟取消时底层客户端清理失败。"""
            yield LLMTextDelta("已输出")
            try:
                await asyncio.Event().wait()
            finally:
                raise RuntimeError("client_cleanup_failed")

    service = AnswerGenerationService(LLM(), Events(), prompt="回答", options=AnswerStreamingOptions(
        idle_timeout_ms=20, flush_interval_ms=5,
    ))
    with pytest.raises(AnswerGenerationError) as error:
        await service.generate(inputs())
    assert error.value.code == "answer_idle_timeout"
    assert error.value.phase == "answer_generation"
