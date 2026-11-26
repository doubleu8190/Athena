"""Chat Completions 增量重组回归。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import openai
import pytest
from openai import AsyncOpenAI

from adapter.llm.openai_chat_completions import OpenAIChatCompletionsAdapter, _retry_after_seconds
from domain.llm.model import (
    LlmCapabilityError,
    LlmImagePart,
    LlmIncompleteError,
    LlmMessage,
    LlmProtocolError,
    LlmProviderError,
    LlmRefusalError,
    LlmRequest,
    LlmRequestError,
    LlmRole,
    LlmText,
    LlmTool,
    LlmToolCall,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
)
from domain.llm.model.profile import ModelProfileVersion, RuntimeModelProfile


async def async_chunks(values: Sequence[object]) -> AsyncIterator[object]:
    """将测试切片按 SDK 异步流接口返回。"""
    for value in values:
        yield value


def runtime_profile() -> RuntimeModelProfile:
    """创建不依赖外部凭据的测试配置。"""
    return RuntimeModelProfile(
        version=ModelProfileVersion(
            id="llmprof_test",
            name="test",
            protocol="chat_completions",
            base_url="https://llm.example/v1",
            model="test-model",
            has_api_key=True,
            context_window_tokens=8192,
            tokenizer_encoding="o200k_base",
            supports_stream=True,
            supports_tools=True,
            max_output_tokens=512,
            request_timeout_seconds=30,
            execution_timeout_seconds=60,
            max_concurrency=1,
            temperature=None,
            enabled=True,
            created_at=datetime.now(UTC),
        ),
        api_key="test-secret",
    )


def chunk(
    *,
    index: int = 0,
    content: str | None = None,
    finish_reason: str | None = None,
    usage: object | None = None,
    tool_calls: list[object] | None = None,
) -> SimpleNamespace:
    """构造与 SDK 分片形状一致的轻量测试对象。"""
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(index=index, delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(id="chatcmpl_test", choices=[choice], usage=usage)


def test_stream_keeps_reading_usage_chunk_after_finish() -> None:
    """finish_reason 后仍读取 usage-only 尾分片再发正常结束。"""
    adapter = OpenAIChatCompletionsAdapter()
    stream: list[object] = [
        chunk(content="hello "),
        chunk(content="world", finish_reason="stop"),
        SimpleNamespace(
            id="chatcmpl_test",
            choices=[],
            usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2, total_tokens=9),
        ),
    ]

    async def collect() -> list[object]:
        return [
            event
            async for event in adapter._events(
                async_chunks(stream),
                runtime_profile(),
            )
        ]

    events = asyncio.run(collect())
    assert [event.text for event in events if isinstance(event, TextDelta)] == ["hello ", "world"]
    completed = next(event for event in events if isinstance(event, ResponseCompleted))
    assert completed.response.usage is not None
    assert completed.response.usage.total_tokens == 9


def test_stream_reassembles_and_validates_tool_call() -> None:
    """跨分片调用参数重组并先校验 Schema 再报告完成。"""
    adapter = OpenAIChatCompletionsAdapter()
    first = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="request_user_input", arguments='{"question":'),
    )
    second = SimpleNamespace(
        index=0, id=None, function=SimpleNamespace(name=None, arguments='"month"}')
    )

    async def collect() -> list[object]:
        return [
            event
            async for event in adapter._events(
                async_chunks(
                    [
                        chunk(tool_calls=[first]),
                        chunk(tool_calls=[second], finish_reason="tool_calls"),
                    ]
                ),
                runtime_profile(),
            )
        ]

    events = asyncio.run(collect())
    assert any(isinstance(event, ToolCallStarted) for event in events)
    completed = next(event for event in events if isinstance(event, ToolCallCompleted))
    assert completed.tool_call.arguments == {"question": "month"}
    assert isinstance(events[-1], ResponseCompleted)


def test_stream_rejects_duplicate_tool_call_id_across_indices() -> None:
    """不同 index 重复使用同一 call_id 时按协议错误拒绝。"""
    adapter = OpenAIChatCompletionsAdapter()

    async def collect() -> None:
        async for _ in adapter._events(
            async_chunks(
                [
                    chunk(
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_duplicate",
                                function=SimpleNamespace(name="tool", arguments='{"a":1}'),
                            ),
                            SimpleNamespace(
                                index=1,
                                id="call_duplicate",
                                function=SimpleNamespace(name="tool", arguments='{"b":2}'),
                            ),
                        ]
                    ),
                    chunk(finish_reason="tool_calls"),
                ]
            ),
            runtime_profile(),
        ):
            pass

    with pytest.raises(LlmProtocolError, match="llm_tool_call_identity_invalid"):
        asyncio.run(collect())


def test_stream_preserves_repeated_tool_identity_without_concatenation() -> None:
    """跨分片重复声明同一 ID/名称时保留原值，冲突身份按协议错误拒绝。"""
    adapter = OpenAIChatCompletionsAdapter()
    first = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="tool", arguments='{"value":'),
    )
    second = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="tool", arguments='"ok"}'),
    )
    conflicting = SimpleNamespace(
        index=0,
        id="call_2",
        function=SimpleNamespace(name="tool", arguments="{}"),
    )

    async def collect(fragments: Sequence[object]) -> list[object]:
        return [
            event
            async for event in adapter._events(
                async_chunks(
                    [chunk(tool_calls=[fragment]) for fragment in fragments]
                    + [chunk(finish_reason="tool_calls")]
                ),
                runtime_profile(),
            )
        ]

    events = asyncio.run(collect([first, second]))
    completed = next(event for event in events if isinstance(event, ToolCallCompleted))
    assert completed.tool_call.call_id == "call_1"
    assert completed.tool_call.name == "tool"
    assert completed.tool_call.arguments == {"value": "ok"}

    with pytest.raises(LlmProtocolError, match="llm_tool_call_identity_invalid"):
        asyncio.run(collect([first, conflicting]))


def test_stream_without_normal_finish_is_incomplete() -> None:
    """没有正常 finish_reason 时不能发 ResponseCompleted。"""
    adapter = OpenAIChatCompletionsAdapter()

    async def collect() -> list[object]:
        return [
            event
            async for event in adapter._events(
                async_chunks([chunk(content="partial")]),
                runtime_profile(),
            )
        ]

    with pytest.raises(LlmIncompleteError):
        asyncio.run(collect())


@pytest.mark.parametrize(
    "choices",
    (
        [
            SimpleNamespace(
                index=0,
                delta=SimpleNamespace(content="one", tool_calls=None),
                finish_reason="stop",
            ),
            SimpleNamespace(
                index=1,
                delta=SimpleNamespace(content="two", tool_calls=None),
                finish_reason="stop",
            ),
        ],
        [
            SimpleNamespace(
                index=1,
                delta=SimpleNamespace(content="wrong", tool_calls=None),
                finish_reason="stop",
            )
        ],
    ),
)
def test_stream_rejects_multiple_or_nonzero_choice_index(choices: list[object]) -> None:
    """单请求只能接受唯一 index=0 的 choice，避免候选正文混入同一响应。"""
    adapter = OpenAIChatCompletionsAdapter()
    malformed = SimpleNamespace(id="chatcmpl_bad", choices=choices, usage=None)

    async def collect() -> None:
        async for _ in adapter._events(
            async_chunks([malformed]),
            runtime_profile(),
        ):
            pass

    with pytest.raises(LlmProtocolError, match="llm_stream_choice_invalid"):
        asyncio.run(collect())


def test_stream_tool_calls_finish_requires_nonempty_calls() -> None:
    adapter = OpenAIChatCompletionsAdapter()

    async def collect() -> None:
        async for _ in adapter._events(
            async_chunks([chunk(finish_reason="tool_calls")]),
            runtime_profile(),
        ):
            pass

    with pytest.raises(LlmProtocolError, match="llm_tool_calls_missing"):
        asyncio.run(collect())


def test_provider_context_limit_error_uses_structured_code_only() -> None:
    """按上游结构化 code 识别上下文超限，不读取敏感错误正文。"""
    response = httpx2.Response(400, request=httpx2.Request("POST", "https://llm.example"))
    error = openai.BadRequestError(
        "sensitive provider message",
        response=response,
        body={"error": {"code": "context_length_exceeded"}},
    )
    with pytest.raises(LlmRequestError, match="llm_context_window_exceeded"):
        OpenAIChatCompletionsAdapter._raise_provider_error(error)


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda request: openai.APITimeoutError(request=request),
        lambda request: openai.APIConnectionError(message="connection dropped", request=request),
    ],
)
def test_complete_timeout_and_connection_errors_are_not_retry_safe(
    error_factory: Callable[[httpx2.Request], Exception],
) -> None:
    """非流式超时或连接中断的执行结果不确定，映射为不可安全重试。"""
    request = httpx2.Request("POST", "https://llm.example.test/v1/chat/completions")
    with pytest.raises(LlmProviderError) as error:
        OpenAIChatCompletionsAdapter._raise_provider_error(error_factory(request))
    assert error.value.retry_safe is False


class _ClosableStream:
    """模拟 OpenAI SDK 异步流，记录显式关闭。"""

    def __init__(self, values: Sequence[object]) -> None:
        self.values = values
        self.closed = False

    def __aiter__(self) -> AsyncIterator[object]:
        return async_chunks(self.values).__aiter__()

    async def close(self) -> None:
        self.closed = True


class _Create:
    """模拟 SDK create 方法。"""

    def __init__(self, stream: _ClosableStream) -> None:
        self.stream = stream

    async def __call__(self, **kwargs: object) -> _ClosableStream:
        return self.stream


def _public_adapter(stream: _ClosableStream) -> OpenAIChatCompletionsAdapter:
    adapter = OpenAIChatCompletionsAdapter()
    adapter._client = lambda runtime: cast(  # type: ignore[method-assign]
        AsyncOpenAI,
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_Create(stream)))),
    )
    return adapter


def test_public_stream_preserves_tool_call_then_tool_result_order() -> None:
    """恢复上下文中的 assistant 调用和 tool 结果形成合法顺序。"""
    calls = (LlmToolCall("call_1", "request_user_input", {"question": "Which?"}),)
    messages = (
        LlmMessage(LlmRole.ASSISTANT, tool_calls=calls),
        LlmMessage(LlmRole.TOOL, '{"answer":"A"}', tool_call_id="call_1"),
        LlmMessage(LlmRole.USER, "continue"),
    )
    serialized = OpenAIChatCompletionsAdapter._messages(messages)
    assert serialized[0]["tool_calls"][0]["id"] == "call_1"
    assert serialized[1] == {"role": "tool", "tool_call_id": "call_1", "content": '{"answer":"A"}'}
    assert serialized[2] == {"role": "user", "content": "continue"}


def test_public_stream_keeps_assistant_text_with_its_tool_calls() -> None:
    """同一条助手说明与工具调用保持在一个 Chat Completions 消息中。"""
    calls = (LlmToolCall("call_1", "mcp_lookup", {"query": "value"}),)
    messages = (
        LlmMessage(LlmRole.ASSISTANT, "我先查询资料。", tool_calls=calls),
        LlmMessage(LlmRole.TOOL, '{"ok":true}', tool_call_id="call_1"),
    )
    serialized = OpenAIChatCompletionsAdapter._messages(messages)
    assert serialized == [
        {
            "role": "assistant",
            "content": "我先查询资料。",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "mcp_lookup",
                        "arguments": '{"query":"value"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok":true}'},
    ]


def test_public_stream_passes_profile_timeout_on_each_sdk_request() -> None:
    """流式调用显式传递 Profile deadline，不依赖 SDK 固定的 600 秒默认值。"""
    stream = _ClosableStream([chunk(content="done", finish_reason="stop")])
    captured: dict[str, object] = {}

    class _CapturingCreate:
        async def __call__(self, **kwargs: object) -> _ClosableStream:
            captured.update(kwargs)
            return stream

    adapter = OpenAIChatCompletionsAdapter()
    adapter._client = lambda runtime: cast(  # type: ignore[method-assign]
        AsyncOpenAI,
        SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_CapturingCreate()))
        ),
    )
    base = runtime_profile()
    runtime = replace(
        base,
        version=replace(base.version, request_timeout_seconds=900),
    )
    request = LlmRequest(
        "unused",
        (LlmMessage(LlmRole.USER, "hi"),),
        timeout_seconds=1200,
    )

    async def consume() -> None:
        async with adapter.stream(runtime, request) as events:
            _ = [event async for event in events]

    asyncio.run(consume())

    assert captured["timeout"] == 900
    assert stream.closed is True


def test_public_stream_passes_through_consumer_error_and_closes_stream() -> None:
    """调用方处理事件时出错原样透传，并关闭 SDK 流。"""
    stream = _ClosableStream([chunk(content="hello")])
    adapter = _public_adapter(stream)
    request = LlmRequest("unused", (LlmMessage(LlmRole.USER, "hi"),))

    async def consume() -> None:
        async with adapter.stream(runtime_profile(), request) as events:
            async for _ in events:
                raise ValueError("caller bug")

    with pytest.raises(ValueError, match="caller bug"):
        asyncio.run(consume())
    assert stream.closed
    assert adapter._semaphores["llmprof_test"]._value == 1


def test_public_stream_passes_through_consumer_timeout_object() -> None:
    """调用方抛出的 TimeoutError 不属于请求超时，必须保持原对象透传。"""
    stream = _ClosableStream([chunk(content="hello")])
    adapter = _public_adapter(stream)
    request = LlmRequest("unused", (LlmMessage(LlmRole.USER, "hi"),))
    consumer_timeout = TimeoutError("caller timeout")

    async def consume() -> None:
        async with adapter.stream(runtime_profile(), request) as events:
            async for _ in events:
                raise consumer_timeout

    with pytest.raises(TimeoutError) as error:
        asyncio.run(consume())
    assert error.value is consumer_timeout
    assert stream.closed
    assert adapter._semaphores["llmprof_test"]._value == 1


def test_public_stream_maps_sdk_iteration_error_and_closes_stream() -> None:
    """上游迭代边界错误映射为安全供应商错误并关闭流。"""

    class _FailingStream(_ClosableStream):
        def __aiter__(self) -> AsyncIterator[object]:
            return self

        async def __anext__(self) -> object:
            raise ValueError("upstream payload")

    stream = _FailingStream([])
    adapter = _public_adapter(stream)
    request = LlmRequest("unused", (LlmMessage(LlmRole.USER, "hi"),))

    async def consume() -> None:
        async with adapter.stream(runtime_profile(), request) as events:
            async for _ in events:
                pass

    with pytest.raises(LlmProviderError) as error:
        asyncio.run(consume())
    assert error.value.code == "llm_provider_error"
    assert stream.closed


@pytest.mark.parametrize("entrypoint", ["complete", "stream"])
def test_request_timeout_while_waiting_for_profile_capacity(entrypoint: str) -> None:
    """等待并发配额超时按请求超时处理，且不会误释放他人占用的名额。"""

    async def run() -> None:
        adapter = OpenAIChatCompletionsAdapter()
        runtime = runtime_profile()
        semaphore = adapter._semaphore_for(runtime.version)
        await semaphore.acquire()
        request = LlmRequest(
            "unused",
            (LlmMessage(LlmRole.USER, "hi"),),
            tool_choice="none",
            timeout_seconds=0.01,
        )
        try:
            with pytest.raises(LlmProviderError, match="llm_timeout"):
                if entrypoint == "complete":
                    await adapter.complete(runtime, request)
                else:
                    async with adapter.stream(runtime, request) as events:
                        async for _ in events:
                            pass
            assert semaphore._value == 0
        finally:
            semaphore.release()
        assert semaphore._value == 1

    asyncio.run(run())


def test_refusal_after_text_never_completes_response() -> None:
    """先输出文本后收到 refusal 仍明确失败，不产生成功结束事件。"""
    stream = [chunk(content="I can"), chunk(content=None)]
    stream[1].choices[0].delta.refusal = "cannot help"
    adapter = OpenAIChatCompletionsAdapter()

    emitted: list[object] = []

    async def run() -> None:
        try:
            async for event in adapter._events(async_chunks(stream), runtime_profile()):
                emitted.append(event)
        except LlmRefusalError:
            return
        raise AssertionError("refusal_not_raised")

    asyncio.run(run())
    assert not any(isinstance(event, ResponseCompleted) for event in emitted)


@pytest.mark.parametrize("field,value", [("supports_stream", False), ("supports_tools", False)])
def test_public_stream_rejects_unsupported_capability_before_sdk_call(
    field: str, value: bool
) -> None:
    """调用前阻止不具备 Run 所需能力的模型版本。"""
    from dataclasses import replace

    runtime = runtime_profile()
    updated_version = (
        replace(runtime.version, supports_stream=value)
        if field == "supports_stream"
        else replace(runtime.version, supports_tools=value)
    )
    runtime = replace(runtime, version=updated_version)
    stream = _ClosableStream([])
    adapter = _public_adapter(stream)
    request = LlmRequest("unused", (LlmMessage(LlmRole.USER, "hi"),))

    async def consume() -> None:
        async with adapter.stream(runtime, request) as events:
            async for _ in events:
                pass

    with pytest.raises(LlmCapabilityError):
        asyncio.run(consume())
    assert not stream.closed


def test_retry_after_parses_seconds_http_date_and_rejects_invalid() -> None:
    """保留有效 Retry-After 数字与日期，忽略无效及缺失值。"""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert _retry_after_seconds("12", now) == 12
    assert _retry_after_seconds("Thu, 01 Jan 2026 00:00:30 GMT", now) == 30
    assert _retry_after_seconds("invalid", now) is None
    assert _retry_after_seconds(None, now) is None


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("MAX_STREAM_CHUNKS", 1, "llm_stream_too_large"),
        ("MAX_TEXT_BYTES", 2, "llm_response_too_large"),
    ],
)
def test_stream_limits_reject_oversized_events_and_close_upstream(
    monkeypatch: pytest.MonkeyPatch, field: str, value: int, code: str
) -> None:
    """分片数与累计文本超限时返回安全错误并关闭上游。"""
    import adapter.llm.openai_chat_completions as chat_adapter

    monkeypatch.setattr(chat_adapter, field, value)
    stream = _ClosableStream([chunk(content="long response"), chunk(finish_reason="stop")])
    adapter = _public_adapter(stream)
    request = LlmRequest("unused", (LlmMessage(LlmRole.USER, "hi"),))

    async def consume() -> None:
        async with adapter.stream(runtime_profile(), request) as events:
            async for _ in events:
                pass

    with pytest.raises(LlmProtocolError) as error:
        asyncio.run(consume())
    assert error.value.code == code
    assert stream.closed


def test_tool_argument_and_call_count_limits() -> None:
    """工具参数字节数和并行调用数都受限。"""
    import adapter.llm.openai_chat_completions as chat_adapter

    adapter = OpenAIChatCompletionsAdapter()
    call = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="tool", arguments="123456"),
    )

    async def arguments() -> None:
        async for _ in adapter._events(async_chunks([chunk(tool_calls=[call])]), runtime_profile()):
            pass

    old_limit = chat_adapter.MAX_TOOL_ARGUMENT_BYTES
    chat_adapter.MAX_TOOL_ARGUMENT_BYTES = 2
    try:
        with pytest.raises(LlmProtocolError, match="llm_tool_arguments_too_large"):
            asyncio.run(arguments())
    finally:
        chat_adapter.MAX_TOOL_ARGUMENT_BYTES = old_limit

    call_chunks = [
        chunk(
            tool_calls=[
                SimpleNamespace(
                    index=index,
                    id=f"call_{index}",
                    function=SimpleNamespace(name="tool", arguments=None),
                )
            ]
        )
        for index in range(9)
    ]

    async def calls() -> None:
        async for _ in adapter._events(async_chunks(call_chunks), runtime_profile()):
            pass

    with pytest.raises(LlmProtocolError, match="llm_stream_too_large"):
        asyncio.run(calls())


def test_tool_argument_limit_is_per_call_for_interleaved_fragments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多个调用交错分片时，各自低于上限且总和超限仍应成功。"""
    import adapter.llm.openai_chat_completions as chat_adapter

    monkeypatch.setattr(chat_adapter, "MAX_TOOL_ARGUMENT_BYTES", 10)
    adapter = OpenAIChatCompletionsAdapter()

    def fragment(
        index: int,
        call_id: str | None,
        name: str | None,
        arguments: str,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            index=index,
            id=call_id,
            function=SimpleNamespace(name=name, arguments=arguments),
        )

    async def collect() -> list[object]:
        return [
            event
            async for event in adapter._events(
                async_chunks(
                    [
                        chunk(tool_calls=[fragment(0, "call_0", "tool", '{"a":')]),
                        chunk(tool_calls=[fragment(1, "call_1", "tool", '{"b":')]),
                        chunk(tool_calls=[fragment(0, None, None, "1}")]),
                        chunk(tool_calls=[fragment(1, None, None, "2}")]),
                        chunk(finish_reason="tool_calls"),
                    ]
                ),
                runtime_profile(),
            )
        ]

    events = asyncio.run(collect())
    completed = [event.tool_call for event in events if isinstance(event, ToolCallCompleted)]
    assert [call.call_id for call in completed] == ["call_0", "call_1"]
    assert [call.arguments for call in completed] == [{"a": 1}, {"b": 2}]
    assert sum(len(json.dumps(call.arguments)) for call in completed) > 10


def test_tool_argument_limit_accumulates_across_fragments_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一调用跨分片累计超限时返回参数过大错误。"""
    import adapter.llm.openai_chat_completions as chat_adapter

    monkeypatch.setattr(chat_adapter, "MAX_TOOL_ARGUMENT_BYTES", 10)
    adapter = OpenAIChatCompletionsAdapter()
    first = SimpleNamespace(
        index=0,
        id="call_0",
        function=SimpleNamespace(name="tool", arguments='{"a":"'),
    )
    second = SimpleNamespace(
        index=0,
        id=None,
        function=SimpleNamespace(name=None, arguments='abcdef"}'),
    )

    async def consume() -> None:
        async for _ in adapter._events(
            async_chunks(
                [
                    chunk(tool_calls=[first]),
                    chunk(tool_calls=[second]),
                    chunk(finish_reason="tool_calls"),
                ]
            ),
            runtime_profile(),
        ):
            pass

    with pytest.raises(LlmProtocolError, match="llm_tool_arguments_too_large"):
        asyncio.run(consume())


def test_stream_assembles_multiple_parallel_tool_calls_by_index() -> None:
    adapter = OpenAIChatCompletionsAdapter()
    calls: list[object] = [
        SimpleNamespace(
            index=0,
            id="call_0",
            function=SimpleNamespace(name="tool", arguments='{"query":"a"}'),
        ),
        SimpleNamespace(
            index=1,
            id="call_1",
            function=SimpleNamespace(name="tool", arguments='{"query":"b"}'),
        ),
    ]

    async def collect() -> list[object]:
        return [
            event
            async for event in adapter._events(
                async_chunks(
                    [
                        chunk(tool_calls=calls),
                        chunk(finish_reason="tool_calls"),
                    ]
                ),
                runtime_profile(),
            )
        ]

    events = asyncio.run(collect())
    completed = [event.tool_call for event in events if isinstance(event, ToolCallCompleted)]
    assert [call.call_id for call in completed] == ["call_0", "call_1"]
    assert [call.arguments for call in completed] == [
        {"query": "a"},
        {"query": "b"},
    ]
    assert isinstance(events[-1], ResponseCompleted)


def test_parallel_tool_call_capability_is_forwarded_to_chat_completions() -> None:
    async def run() -> None:
        requests: list[dict[str, Any]] = []

        async def handle(request: httpx2.Request) -> httpx2.Response:
            requests.append(cast(dict[str, Any], json.loads(request.content)))
            body = (
                'data: {"id":"chatcmpl_parallel","object":"chat.completion.chunk",'
                '"created":1,"model":"test-model","choices":[{"index":0,'
                '"delta":{"content":"done"},"finish_reason":"stop"}]}\n\n'
                'data: {"id":"chatcmpl_parallel","object":"chat.completion.chunk",'
                '"created":1,"model":"test-model","choices":[],"usage":'
                '{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body.encode(),
                request=request,
            )

        base = runtime_profile()
        runtime = replace(
            base,
            version=replace(base.version, supports_parallel_tool_calls=True),
        )
        adapter = OpenAIChatCompletionsAdapter()
        sdk = AsyncOpenAI(
            api_key=runtime.api_key,
            base_url=runtime.version.base_url,
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
        )
        adapter._clients[
            (
                runtime.version.base_url,
                hashlib.sha256(runtime.api_key.encode()).hexdigest(),
            )
        ] = sdk
        request = LlmRequest(
            "unused",
            (LlmMessage(LlmRole.USER, "call tools"),),
            tools=(
                LlmTool(
                    "tool",
                    "Tool",
                    {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                ),
            ),
        )

        async with adapter.stream(runtime, request) as events:
            _ = [event async for event in events]

        assert requests[0]["parallel_tool_calls"] is True
        await adapter.aclose()

    asyncio.run(run())


def test_sdk_wire_final_request_uses_no_tools_and_disables_retries() -> None:
    """最终回答轮在真实 SDK 请求中显式关闭工具，且客户端不隐式重试。"""

    async def run() -> None:
        requests: list[dict[str, Any]] = []

        async def handle(request: httpx2.Request) -> httpx2.Response:
            requests.append(cast(dict[str, Any], json.loads(request.content)))
            body = (
                'data: {"id":"chatcmpl_final","object":"chat.completion.chunk",'
                '"created":1,"model":"test-model","choices":[{"index":0,'
                '"delta":{"content":"final"},"finish_reason":"stop"}]}\n\n'
                'data: {"id":"chatcmpl_final","object":"chat.completion.chunk",'
                '"created":1,"model":"test-model","choices":[],"usage":'
                '{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body.encode(),
                request=request,
            )

        runtime = runtime_profile()
        adapter = OpenAIChatCompletionsAdapter()
        sdk = AsyncOpenAI(
            api_key=runtime.api_key,
            base_url=runtime.version.base_url,
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
        )
        adapter._clients[
            (
                runtime.version.base_url,
                hashlib.sha256(runtime.api_key.encode()).hexdigest(),
            )
        ] = sdk
        request = LlmRequest(
            "unused",
            (LlmMessage(LlmRole.USER, "answer now"),),
            tool_choice="none",
        )

        async with adapter.stream(runtime, request) as events:
            _ = [event async for event in events]

        assert sdk.max_retries == 0
        assert len(requests) == 1
        assert "tools" not in requests[0]
        assert "tool_choice" not in requests[0]
        assert "parallel_tool_calls" not in requests[0]
        await adapter.aclose()

    asyncio.run(run())


def test_sdk_wire_stream_request_keeps_tools_and_tool_choice_none_when_tools_provided() -> None:
    """提供工具且 tool_choice='none' 时，流式请求正常携带 tools 与 tool_choice='none'。"""

    async def run() -> None:
        requests: list[dict[str, Any]] = []

        async def handle(request: httpx2.Request) -> httpx2.Response:
            requests.append(cast(dict[str, Any], json.loads(request.content)))
            body = (
                'data: {"id":"chatcmpl_none","object":"chat.completion.chunk",'
                '"created":1,"model":"test-model","choices":[{"index":0,'
                '"delta":{"content":"done"},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body.encode(),
                request=request,
            )

        runtime = runtime_profile()
        adapter = OpenAIChatCompletionsAdapter()
        sdk = AsyncOpenAI(
            api_key=runtime.api_key,
            base_url=runtime.version.base_url,
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
        )
        adapter._clients[
            (
                runtime.version.base_url,
                hashlib.sha256(runtime.api_key.encode()).hexdigest(),
            )
        ] = sdk
        request = LlmRequest(
            "unused",
            (LlmMessage(LlmRole.USER, "answer without calling tools"),),
            tools=(
                LlmTool(
                    "lookup",
                    "Lookup helper",
                    {"type": "object", "properties": {}, "additionalProperties": False},
                ),
            ),
            tool_choice="none",
        )

        async with adapter.stream(runtime, request) as events:
            _ = [event async for event in events]

        assert len(requests) == 1
        assert len(requests[0]["tools"]) == 1
        assert requests[0]["tools"][0]["function"]["name"] == "lookup"
        assert requests[0]["tool_choice"] == "none"
        assert requests[0]["parallel_tool_calls"] is False
        await adapter.aclose()

    asyncio.run(run())


def test_adapter_defers_unknown_tool_and_schema_validation_to_execution() -> None:
    """工具不存在或参数不符合目录 Schema 时仍生成调用，由执行层记录拒绝。"""
    adapter = OpenAIChatCompletionsAdapter()
    fragment = SimpleNamespace(
        index=0,
        id="call_unknown",
        function=SimpleNamespace(
            name="model_guessed_tool",
            arguments='{"unexpected":true}',
        ),
    )

    async def collect() -> list[object]:
        return [
            event
            async for event in adapter._events(
                async_chunks(
                    [
                        chunk(tool_calls=[fragment]),
                        chunk(finish_reason="tool_calls"),
                    ]
                ),
                runtime_profile(),
            )
        ]

    events = asyncio.run(collect())
    completed = next(event for event in events if isinstance(event, ToolCallCompleted))
    assert completed.tool_call.name == "model_guessed_tool"
    assert completed.tool_call.arguments == {"unexpected": True}


@pytest.mark.parametrize(
    ("call_id", "arguments"),
    [
        (None, '{"query":"ok"}'),
        ("call_invalid_json", '{"query":'),
        ("call_non_object", '["query"]'),
    ],
)
def test_adapter_keeps_protocol_failures_for_missing_id_or_invalid_arguments(
    call_id: str | None,
    arguments: str,
) -> None:
    """缺失调用 ID、非法 JSON 和非对象参数仍是不可进入执行层的协议错误。"""
    adapter = OpenAIChatCompletionsAdapter()
    fragment = SimpleNamespace(
        index=0,
        id=call_id,
        function=SimpleNamespace(name="known_tool", arguments=arguments),
    )

    async def collect() -> None:
        async for _event in adapter._events(
            async_chunks(
                [
                    chunk(tool_calls=[fragment]),
                    chunk(finish_reason="tool_calls"),
                ]
            ),
            runtime_profile(),
        ):
            pass

    with pytest.raises(LlmProtocolError):
        asyncio.run(collect())


def test_public_stream_early_exit_and_cancellation_close_sdk_stream() -> None:
    """调用方提前退出或取消消费时关闭流并释放配额。"""

    class _BlockingStream(_ClosableStream):
        def __aiter__(self) -> AsyncIterator[object]:
            return self

        async def __anext__(self) -> object:
            await asyncio.Event().wait()
            raise StopAsyncIteration

    stream = _ClosableStream([chunk(content="hello"), chunk(finish_reason="stop")])
    adapter = _public_adapter(stream)
    request = LlmRequest("unused", (LlmMessage(LlmRole.USER, "hi"),))

    async def early_exit() -> None:
        async with adapter.stream(runtime_profile(), request) as events:
            async for _ in events:
                break

    asyncio.run(early_exit())
    assert stream.closed
    assert adapter._semaphores["llmprof_test"]._value == 1

    blocking = _BlockingStream([])
    adapter = _public_adapter(blocking)

    async def cancel() -> None:
        async with adapter.stream(runtime_profile(), request) as events:
            task = asyncio.ensure_future(anext(events))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(cancel())
    assert blocking.closed
    assert adapter._semaphores["llmprof_test"]._value == 1


def test_complete_allows_runtime_without_stream_or_tool_capabilities() -> None:
    """压缩使用非流式文本能力，不依赖主回答的 stream/tools 标记。"""
    adapter = OpenAIChatCompletionsAdapter()
    original = runtime_profile()
    runtime = RuntimeModelProfile(
        version=replace(original.version, supports_stream=False, supports_tools=False),
        api_key=original.api_key,
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="summary", refusal=None, tool_calls=None),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12),
        model="test-model",
        id="response-1",
    )

    async def create(**kwargs: object) -> object:
        assert kwargs["stream"] is False
        assert "tools" not in kwargs
        assert "tool_choice" not in kwargs
        assert "parallel_tool_calls" not in kwargs
        assert kwargs["timeout"] == runtime.version.request_timeout_seconds
        return response

    object.__setattr__(
        adapter,
        "_client",
        lambda _: cast(
            AsyncOpenAI,
            SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        ),
    )
    result = asyncio.run(
        adapter.complete(
            runtime,
            LlmRequest(
                "unused",
                (LlmMessage(LlmRole.USER, "history"),),
                tool_choice="none",
                max_output_tokens=20,
            ),
        )
    )
    assert isinstance(result.output[0], LlmText)
    assert result.output[0].text == "summary"
    assert result.usage is not None and result.usage.total_tokens == 12


@pytest.mark.parametrize(
    "bad_base64",
    [
        "",
        " ",
        "   ",
        "\t",
        "\n",
        "\r",
        "\x0b",
        "\x0c",
        " AAAA==",
        "AAAA== ",
        "\tAAAA==",
        "AAAA==\n",
        "AA AA==",
        "AA\tAA==",
        "AA\nAA==",
        "AA\rAA==",
        "AA\x0bAA==",
        "AA\x0cAA==",
        "AA\r\nAA==",
        "AA " * 1000 + "==",
        "中文base64==",
        cast(Any, None),
        cast(Any, 12345),
    ],
)
def test_serialize_content_parts_rejects_all_ascii_whitespace_and_invalid_base64(
    bad_base64: Any,
) -> None:
    """多模态图片 base64 快速 C 层校验必须严格拦截全部 ASCII 空白符、空值、非 ASCII 与非字符串。"""
    message = LlmMessage(
        LlmRole.USER,
        content_parts=(LlmImagePart(mime_type="image/png", data_base64=bad_base64),),
    )
    with pytest.raises(LlmProtocolError) as error:
        OpenAIChatCompletionsAdapter._messages((message,))
    assert error.value.code == "llm_chat_image_invalid"


def test_serialize_content_parts_accepts_valid_large_ascii_base64() -> None:
    """合法无空白纯 ASCII base64（含较大负载）应正常序列化为标准 data URL。"""
    payload = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3ODkw+/" * 1024 + "=="
    message = LlmMessage(
        LlmRole.USER,
        content_parts=(LlmImagePart(mime_type="image/webp", data_base64=payload, detail="low"),),
    )
    serialized = OpenAIChatCompletionsAdapter._messages((message,))
    assert serialized == [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/webp;base64,{payload}",
                        "detail": "low",
                    },
                }
            ],
        }
    ]


def test_client_disables_redirects_allows_compression_and_keeps_chunk_cap() -> None:
    """验证 _client 使用标准 httpx2 客户端（禁重定向、允许压缩协商）且保留 65536 分片上限。"""
    import adapter.llm.openai_chat_completions as chat_adapter

    assert chat_adapter.MAX_STREAM_CHUNKS == 65536

    async def run() -> None:
        adapter = OpenAIChatCompletionsAdapter()
        sdk = adapter._client(runtime_profile())
        try:
            http_client = sdk._client
            assert http_client.follow_redirects is False
            assert http_client.headers.get("accept-encoding", "").lower() != "identity"
        finally:
            await adapter.aclose()

    asyncio.run(run())
