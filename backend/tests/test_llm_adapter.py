"""Responses Adapter 的协议、凭据隔离与流资源测试。"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI
from openai.types.responses import Response, ResponseOutputMessage

from adapter.llm.openai_responses import (
    OpenAIResponsesAdapter,
    ResponseLimitError,
    _BoundedResponseStream,
)
from app.llm.config import LlmProfile
from domain.llm.model import (
    LlmIncompleteError,
    LlmMessage,
    LlmProtocolError,
    LlmProviderError,
    LlmRefusalError,
    LlmRequest,
    LlmRole,
    LlmTool,
    LlmToolCall,
    ResponseCompleted,
    TextDelta,
    ToolCallCompleted,
)


def profile(name: str, key: str, *, capacity: int = 1) -> LlmProfile:
    """构造适配器测试 Profile。"""
    return LlmProfile(
        name, key, "model", None, frozenset({"stream", "tools"}), max_concurrency=capacity
    )


def response() -> Response:
    """构造固定的成功 Responses 样本。"""
    return Response.model_validate(
        {
            "id": "resp_1",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "model",
            "output": [],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": None,
        }
    )


class FakeStream:
    """按顺序发出协议样本并记录关闭。"""

    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.closed = False

    def __aiter__(self) -> FakeStream:
        return self

    async def __anext__(self) -> Any:
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)

    async def close(self) -> None:
        self.closed = True


def test_input_preserves_function_call_and_tool_result_order() -> None:
    """显式上下文续接项和 tool result 按原顺序传递。"""
    messages = (
        LlmMessage(
            LlmRole.ASSISTANT, continuation_items=({"type": "function_call", "call_id": "c1"},)
        ),
        LlmMessage(LlmRole.TOOL, "result", tool_call_id="c1"),
    )
    assert OpenAIResponsesAdapter._input(messages) == [
        {"type": "function_call", "call_id": "c1"},
        {"type": "function_call_output", "call_id": "c1", "output": "result"},
    ]


def test_typed_tool_calls_and_results_remain_responses_compatible() -> None:
    """新增 typed assistant tool_calls 仍映射为 Responses call/output 顺序。"""
    messages = (
        LlmMessage(
            LlmRole.ASSISTANT,
            tool_calls=(LlmToolCall("call_1", "lookup", {"key": "value"}),),
        ),
        LlmMessage(LlmRole.TOOL, "result", tool_call_id="call_1"),
    )
    assert OpenAIResponsesAdapter._input(messages) == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": '{"key":"value"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "result"},
    ]


def test_clients_do_not_share_different_credentials() -> None:
    """相同连接地址下不同凭据各自创建 SDK 客户端且禁用重试。"""
    adapter = OpenAIResponsesAdapter(
        {"one": profile("one", "secret-one"), "two": profile("two", "secret-two")}
    )
    first = adapter._client(adapter._profile("one"))
    second = adapter._client(adapter._profile("two"))
    assert first is not second
    assert first.max_retries == second.max_retries == 0
    asyncio.run(adapter.aclose())


def test_openai_sdk_mock_transport_sends_one_stateless_responses_request() -> None:
    """真实 SDK 通过 MockTransport 只发送一次显式上下文请求。"""

    async def run() -> None:
        calls: list[dict[str, Any]] = []

        async def handle(request: httpx2.Request) -> httpx2.Response:
            import json

            calls.append(json.loads(request.content))
            return httpx2.Response(
                200,
                headers={"x-request-id": "req_test"},
                json={
                    "id": "resp_1",
                    "object": "response",
                    "created_at": 1,
                    "status": "completed",
                    "model": "model",
                    "output": [],
                    "parallel_tool_calls": True,
                    "tool_choice": "auto",
                    "tools": [],
                    "usage": None,
                },
                request=request,
            )

        profile_config = profile("assistant", "secret")
        adapter = OpenAIResponsesAdapter({"assistant": profile_config})
        sdk_http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
        sdk = AsyncOpenAI(
            api_key="secret",
            base_url="https://api.openai.com/v1",
            max_retries=0,
            http_client=sdk_http,
        )
        key = (
            "https://api.openai.com/v1",
            hashlib.sha256(b"secret").hexdigest(),
            profile_config.max_response_bytes,
        )
        adapter._clients[key] = sdk
        request = LlmRequest("assistant", (LlmMessage(LlmRole.USER, "hello"),), temperature=0.25)
        result = await adapter.complete("assistant", request)
        assert result.status == "completed"
        assert result.request_id == "req_test"
        assert len(calls) == 1
        assert calls[0]["store"] is False
        assert calls[0]["temperature"] == 0.25
        assert calls[0]["input"] == [{"role": "user", "content": "hello"}]
        assert "previous_response_id" not in calls[0]
        await adapter.aclose()

    asyncio.run(run())


def test_non_stream_entrypoints_keep_text_and_structured_wire_options_distinct() -> None:
    """普通文本请求携带工具与 reasoning include，结构化请求只增加 text.format。"""

    async def run() -> None:
        calls: list[dict[str, Any]] = []

        async def handle(request: httpx2.Request) -> httpx2.Response:
            calls.append(json.loads(request.content))
            return httpx2.Response(
                200,
                json={
                    "id": f"resp_{len(calls)}",
                    "object": "response",
                    "created_at": 1,
                    "status": "completed",
                    "model": "model",
                    "output": [],
                    "parallel_tool_calls": False,
                    "tool_choice": "auto",
                    "tools": [],
                    "usage": None,
                },
                request=request,
            )

        profile_config = profile("assistant", "secret")
        adapter = OpenAIResponsesAdapter({"assistant": profile_config})
        sdk_http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
        sdk = AsyncOpenAI(
            api_key="secret",
            base_url="https://api.openai.com/v1",
            max_retries=0,
            http_client=sdk_http,
        )
        adapter._clients[
            (
                "https://api.openai.com/v1",
                hashlib.sha256(b"secret").hexdigest(),
                profile_config.max_response_bytes,
            )
        ] = sdk
        tool = LlmTool(
            "lookup",
            "lookup records",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )
        await adapter.complete(
            "assistant",
            LlmRequest(
                "assistant",
                (LlmMessage(LlmRole.USER, "hello"),),
                tools=(tool,),
                tool_choice="tool:lookup",
                max_output_tokens=64,
                temperature=0.25,
            ),
        )
        await adapter.generate_structured(
            "assistant",
            LlmRequest(
                "assistant",
                (LlmMessage(LlmRole.USER, "hello"),),
                max_output_tokens=64,
                temperature=0.25,
            ),
            "answer",
            {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        )
        await adapter.aclose()

        text_request, structured_request = calls
        assert text_request["tools"][0]["name"] == "lookup"
        assert text_request["tool_choice"] == {"type": "function", "name": "lookup"}
        assert text_request["parallel_tool_calls"] is False
        assert text_request["include"] == ["reasoning.encrypted_content"]
        assert structured_request["text"]["format"]["name"] == "answer"
        assert "tools" not in structured_request
        assert "tool_choice" not in structured_request
        assert "parallel_tool_calls" not in structured_request
        assert "include" not in structured_request
        for request_payload in calls:
            assert request_payload["store"] is False
            assert request_payload["max_output_tokens"] == 64
            assert request_payload["temperature"] == 0.25

    asyncio.run(run())


@pytest.mark.parametrize("entrypoint", ["complete", "structured"])
def test_non_stream_parameter_construction_errors_are_mapped(entrypoint: str) -> None:
    """两个非流式入口均把参数构造阶段的非 LlmError 映射为固定供应商错误。"""

    async def run() -> None:
        adapter = OpenAIResponsesAdapter({"assistant": profile("assistant", "secret")})
        create_called = False

        class ResponsesStub:
            async def create(self, **_kwargs: Any) -> Any:
                nonlocal create_called
                create_called = True
                raise AssertionError("无效参数不应进入 SDK 调用")

        class ClientStub:
            responses = ResponsesStub()

            async def close(self) -> None:
                return None

        key = (
            "https://api.openai.com/v1",
            hashlib.sha256(b"secret").hexdigest(),
            adapter._profile("assistant").max_response_bytes,
        )
        adapter._clients[key] = ClientStub()  # type: ignore[assignment]
        request = LlmRequest(
            "assistant",
            (
                LlmMessage(
                    LlmRole.ASSISTANT,
                    tool_calls=(LlmToolCall("call_unserializable", "lookup", {"value": object()}),),
                ),
            ),
        )
        with pytest.raises(LlmProviderError, match="llm_provider_error"):
            if entrypoint == "complete":
                await adapter.complete("assistant", request)
            else:
                await adapter.generate_structured(
                    "assistant",
                    request,
                    "answer",
                    {"type": "object", "properties": {}},
                )
        assert create_called is False
        await asyncio.wait_for(adapter._semaphores["assistant"].acquire(), timeout=0.1)
        adapter._semaphores["assistant"].release()
        await adapter.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("entrypoint", ["complete", "structured"])
def test_non_stream_response_conversion_errors_are_mapped(entrypoint: str) -> None:
    """两个非流式入口均把畸形 SDK 响应转换异常映射为固定供应商错误。"""

    async def run() -> None:
        adapter = OpenAIResponsesAdapter({"assistant": profile("assistant", "secret")})

        class ResponsesStub:
            async def create(self, **_kwargs: Any) -> Any:
                return SimpleNamespace(
                    status="completed",
                    output=[],
                    incomplete_details=None,
                    model="model",
                    usage=None,
                )

        class ClientStub:
            responses = ResponsesStub()

            async def close(self) -> None:
                return None

        key = (
            "https://api.openai.com/v1",
            hashlib.sha256(b"secret").hexdigest(),
            adapter._profile("assistant").max_response_bytes,
        )
        adapter._clients[key] = ClientStub()  # type: ignore[assignment]
        request = LlmRequest("assistant", (LlmMessage(LlmRole.USER, "hello"),))
        with pytest.raises(LlmProviderError, match="llm_provider_error"):
            if entrypoint == "complete":
                await adapter.complete("assistant", request)
            else:
                await adapter.generate_structured(
                    "assistant",
                    request,
                    "answer",
                    {"type": "object", "properties": {}},
                )
        await asyncio.wait_for(adapter._semaphores["assistant"].acquire(), timeout=0.1)
        adapter._semaphores["assistant"].release()
        await adapter.aclose()

    asyncio.run(run())


def test_responses_sdk_wire_final_request_uses_no_tools_and_disables_retries() -> None:
    """最终回答轮的真实 Responses 请求显式关闭工具，SDK 不隐式重试。"""

    async def run() -> None:
        requests: list[dict[str, Any]] = []

        async def handle(request: httpx2.Request) -> httpx2.Response:
            requests.append(json.loads(request.content))
            response_payload = {
                "id": "resp_final",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "model",
                "output": [],
                "parallel_tool_calls": False,
                "tool_choice": "none",
                "tools": [],
                "usage": None,
            }
            event = {
                "type": "response.completed",
                "sequence_number": 0,
                "response": response_payload,
            }
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(f"event: response.completed\ndata: {json.dumps(event)}\n\n").encode(),
                request=request,
            )

        cfg = profile("assistant", "secret")
        adapter = OpenAIResponsesAdapter({"assistant": cfg})
        sdk_http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
        sdk = AsyncOpenAI(
            api_key="secret",
            base_url="https://api.openai.com/v1",
            max_retries=0,
            http_client=sdk_http,
        )
        adapter._clients[
            (
                "https://api.openai.com/v1",
                hashlib.sha256(b"secret").hexdigest(),
                cfg.max_response_bytes,
            )
        ] = sdk
        request = LlmRequest(
            "assistant",
            (LlmMessage(LlmRole.USER, "answer now"),),
            tool_choice="none",
        )

        async with adapter.stream("assistant", request) as stream:
            _ = [event async for event in stream]

        assert sdk.max_retries == 0
        assert len(requests) == 1
        assert requests[0]["tools"] == []
        assert requests[0]["tool_choice"] == "none"
        assert requests[0]["parallel_tool_calls"] is False
        await adapter.aclose()

    asyncio.run(run())


def test_responses_adapter_defers_unknown_tool_and_schema_validation() -> None:
    """Responses 同样将未知工具和 Schema 不匹配交给执行层。"""
    result = Response.model_validate(
        {
            "id": "resp_unknown_tool",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "model",
            "output": [
                {
                    "type": "function_call",
                    "id": "fc_unknown",
                    "call_id": "call_unknown",
                    "name": "model_guessed_tool",
                    "arguments": '{"unexpected":true}',
                    "status": "completed",
                }
            ],
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
            "usage": None,
        }
    )
    adapter = OpenAIResponsesAdapter({"assistant": profile("assistant", "secret")})
    response = adapter._response(
        result,
        profile("assistant", "secret"),
    )
    call = next(item for item in response.output if isinstance(item, LlmToolCall))
    assert call.name == "model_guessed_tool"
    assert call.arguments == {"unexpected": True}


@pytest.mark.parametrize(
    ("call_id", "arguments"),
    [
        ("", '{"query":"ok"}'),
        ("call_invalid_json", '{"query":'),
        ("call_non_object", '["query"]'),
    ],
)
def test_responses_adapter_keeps_protocol_failures_for_missing_id_or_invalid_arguments(
    call_id: str,
    arguments: str,
) -> None:
    """Responses 缺失 ID、非法 JSON 和非对象参数仍是协议失败。"""
    result = Response.model_validate(
        {
            "id": "resp_invalid_call",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "model",
            "output": [
                {
                    "type": "function_call",
                    "id": "fc_invalid",
                    "call_id": call_id,
                    "name": "known_tool",
                    "arguments": arguments,
                    "status": "completed",
                }
            ],
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
            "usage": None,
        }
    )
    adapter = OpenAIResponsesAdapter({"assistant": profile("assistant", "secret")})
    with pytest.raises(LlmProtocolError):
        adapter._response(result, profile("assistant", "secret"))


@pytest.mark.parametrize("streaming", [False, True])
def test_sdk_mock_transport_rejects_gzip_before_body_read_and_releases_capacity(
    streaming: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """普通响应和 SSE gzip 均在 SDK 解压前拒绝，关闭连接并释放并发名额。"""

    async def run() -> None:
        class TrackedBody(httpx2.AsyncByteStream):
            def __init__(self, body: bytes) -> None:
                self.body = body
                self.iterated = False
                self.closed = False

            async def __aiter__(self) -> Any:
                self.iterated = True
                yield self.body

            async def aclose(self) -> None:
                self.closed = True

        standard = response().model_dump_json().encode()
        sse_data = {
            "type": "response.completed",
            "sequence_number": 0,
            "response": response().model_dump(mode="json"),
        }
        sse = f"event: response.completed\ndata: {json.dumps(sse_data)}\n\n".encode()
        tracked = TrackedBody(gzip.compress(sse if streaming else standard))
        request_headers: list[str | None] = []

        async def handle(request: httpx2.Request) -> httpx2.Response:
            request_headers.append(request.headers.get("accept-encoding"))
            return httpx2.Response(
                200,
                headers={
                    "content-type": "text/event-stream" if streaming else "application/json",
                    "content-encoding": "gzip",
                },
                stream=tracked,
                request=request,
            )

        cfg = profile("assistant", "secret")
        monkeypatch.setattr(httpx2, "AsyncHTTPTransport", lambda: httpx2.MockTransport(handle))
        adapter = OpenAIResponsesAdapter({"assistant": cfg})
        adapter._client(cfg)
        request = LlmRequest("assistant", (LlmMessage(LlmRole.USER, "hi"),))
        with pytest.raises(LlmProtocolError, match="llm_response_encoding_unsupported"):
            if streaming:
                async with adapter.stream("assistant", request) as events:
                    _ = [event async for event in events]
            else:
                await adapter.complete("assistant", request)
        assert request_headers == ["identity"]
        assert tracked.closed and not tracked.iterated
        await asyncio.wait_for(adapter._semaphores["assistant"].acquire(), timeout=0.01)
        adapter._semaphores["assistant"].release()
        await adapter.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("content_encoding", [None, "identity"])
def test_sdk_mock_transport_accepts_identity_or_empty_encoding(
    content_encoding: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未压缩和明确 identity 响应被接受，且请求声明 Accept-Encoding: identity。"""

    async def run() -> None:
        headers: list[str | None] = []

        async def handle(request: httpx2.Request) -> httpx2.Response:
            headers.append(request.headers.get("accept-encoding"))
            response_headers = (
                {} if content_encoding is None else {"content-encoding": content_encoding}
            )
            return httpx2.Response(
                200,
                headers=response_headers,
                json={
                    "id": "resp_identity",
                    "object": "response",
                    "created_at": 1,
                    "status": "completed",
                    "model": "model",
                    "output": [],
                    "parallel_tool_calls": True,
                    "tool_choice": "auto",
                    "tools": [],
                    "usage": None,
                },
                request=request,
            )

        monkeypatch.setattr(httpx2, "AsyncHTTPTransport", lambda: httpx2.MockTransport(handle))
        cfg = profile("assistant", "secret")
        adapter = OpenAIResponsesAdapter({"assistant": cfg})
        request = LlmRequest("assistant", (LlmMessage(LlmRole.USER, "hi"),))
        result = await adapter.complete("assistant", request)
        assert result.status == "completed"
        assert headers == ["identity"]
        await adapter.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("entrypoint", ["complete", "structured"])
def test_non_stream_timeout_maps_and_releases_profile_capacity(entrypoint: str) -> None:
    """完整与结构化入口共用超时处理后均返回固定错误码并释放并发配额。"""

    async def run() -> None:
        adapter = OpenAIResponsesAdapter({"assistant": profile("assistant", "secret")})

        class ResponsesStub:
            async def create(self, **_kwargs: Any) -> Any:
                await asyncio.sleep(1)
                raise AssertionError("调用应在返回前超时")

        class ClientStub:
            responses = ResponsesStub()

            async def close(self) -> None:
                return None

        key = (
            "https://api.openai.com/v1",
            hashlib.sha256(b"secret").hexdigest(),
            adapter._profile("assistant").max_response_bytes,
        )
        adapter._clients[key] = ClientStub()  # type: ignore[assignment]
        request = LlmRequest(
            "assistant",
            (LlmMessage(LlmRole.USER, "hello"),),
            timeout_seconds=0.01,
        )
        with pytest.raises(LlmProviderError, match="llm_timeout"):
            if entrypoint == "complete":
                await adapter.complete("assistant", request)
            else:
                await adapter.generate_structured(
                    "assistant",
                    request,
                    "answer",
                    {"type": "object", "properties": {}},
                )
        await asyncio.wait_for(adapter._semaphores["assistant"].acquire(), timeout=0.1)
        adapter._semaphores["assistant"].release()
        await adapter.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("entrypoint", ["complete", "structured"])
def test_non_stream_cancellation_propagates_and_releases_profile_capacity(
    entrypoint: str,
) -> None:
    """完整与结构化入口取消后保留 CancelledError 语义并释放并发配额。"""

    async def run() -> None:
        adapter = OpenAIResponsesAdapter({"assistant": profile("assistant", "secret")})
        started = asyncio.Event()

        class ResponsesStub:
            async def create(self, **_kwargs: Any) -> Any:
                started.set()
                await asyncio.sleep(60)
                raise AssertionError("取消应终止请求")

        class ClientStub:
            responses = ResponsesStub()

            async def close(self) -> None:
                return None

        key = (
            "https://api.openai.com/v1",
            hashlib.sha256(b"secret").hexdigest(),
            adapter._profile("assistant").max_response_bytes,
        )
        adapter._clients[key] = ClientStub()  # type: ignore[assignment]
        request = LlmRequest("assistant", (LlmMessage(LlmRole.USER, "hello"),))

        async def invoke() -> None:
            if entrypoint == "complete":
                await adapter.complete("assistant", request)
            else:
                await adapter.generate_structured(
                    "assistant",
                    request,
                    "answer",
                    {"type": "object", "properties": {}},
                )

        task = asyncio.create_task(invoke())
        await asyncio.wait_for(started.wait(), timeout=0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(adapter._semaphores["assistant"].acquire(), timeout=0.1)
        adapter._semaphores["assistant"].release()
        await adapter.aclose()

    asyncio.run(run())


def test_stream_emits_text_and_one_completed_event() -> None:
    """正常终态只发出一次 ResponseCompleted。"""
    adapter = OpenAIResponsesAdapter({"assistant": profile("assistant", "secret")})
    item_added = ResponseOutputMessage.model_validate(
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "in_progress",
            "content": [],
        }
    )
    part = {"type": "output_text", "text": ""}
    final_item = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "hi", "annotations": []}],
    }
    final_response = Response.model_validate(
        {
            "id": "resp_1",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "model",
            "output": [final_item],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": None,
        }
    )
    stream = FakeStream(
        [
            SimpleNamespace(
                type="response.output_item.added",
                output_index=0,
                item=item_added,
            ),
            SimpleNamespace(
                type="response.content_part.added",
                output_index=0,
                item_id="msg_1",
                content_index=0,
                part=part,
            ),
            SimpleNamespace(
                type="response.output_text.delta",
                output_index=0,
                item_id="msg_1",
                content_index=0,
                delta="hi",
            ),
            SimpleNamespace(
                type="response.output_text.done",
                output_index=0,
                item_id="msg_1",
                content_index=0,
                text="hi",
            ),
            SimpleNamespace(
                type="response.content_part.done",
                output_index=0,
                item_id="msg_1",
                content_index=0,
                part={"type": "output_text", "text": "hi"},
            ),
            SimpleNamespace(
                type="response.output_item.done",
                output_index=0,
                item=ResponseOutputMessage.model_validate(final_item),
            ),
            SimpleNamespace(type="response.completed", response=final_response),
        ]
    )

    async def run() -> list[Any]:
        return [event async for event in adapter._events(stream, adapter._profile("assistant"))]

    events = asyncio.run(run())
    assert isinstance(events[0], TextDelta)
    assert sum(isinstance(event, ResponseCompleted) for event in events) == 1


def test_sdk_mock_transport_parses_complete_responses_sse_sequence() -> None:
    """真实 SDK SSE 解码路径处理完整文本生命周期及终态。"""

    async def run() -> list[Any]:
        final_item = {
            "type": "message",
            "id": "msg_sdk",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "hello", "annotations": []}],
        }
        final_response = {
            "id": "resp_sdk",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "model",
            "output": [final_item],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": None,
        }
        payloads = [
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "sequence_number": 0,
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "id": "msg_sdk",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    },
                },
            ),
            (
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "sequence_number": 1,
                    "output_index": 0,
                    "item_id": "msg_sdk",
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                },
            ),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 2,
                    "output_index": 0,
                    "item_id": "msg_sdk",
                    "content_index": 0,
                    "delta": "hello",
                    "logprobs": [],
                },
            ),
            (
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "sequence_number": 3,
                    "output_index": 0,
                    "item_id": "msg_sdk",
                    "content_index": 0,
                    "text": "hello",
                    "logprobs": [],
                },
            ),
            (
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "sequence_number": 4,
                    "output_index": 0,
                    "item_id": "msg_sdk",
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "hello", "annotations": []},
                },
            ),
            (
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "sequence_number": 5,
                    "output_index": 0,
                    "item": final_item,
                },
            ),
            (
                "response.completed",
                {"type": "response.completed", "sequence_number": 6, "response": final_response},
            ),
        ]
        body = b"".join(
            f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode() for name, payload in payloads
        )

        async def handle(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, content=body, request=request
            )

        adapter = OpenAIResponsesAdapter({"assistant": profile("assistant", "secret")})
        sdk_http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
        sdk = AsyncOpenAI(
            api_key="secret",
            base_url="https://api.openai.com/v1",
            max_retries=0,
            http_client=sdk_http,
        )
        key = (
            "https://api.openai.com/v1",
            hashlib.sha256(b"secret").hexdigest(),
            profile("assistant", "secret").max_response_bytes,
        )
        adapter._clients[key] = sdk
        request = LlmRequest("assistant", (LlmMessage(LlmRole.USER, "hi"),))
        async with adapter.stream("assistant", request) as events:
            result = [event async for event in events]
        await adapter.aclose()
        return result

    events = asyncio.run(run())
    assert any(isinstance(event, TextDelta) and event.text == "hello" for event in events)
    assert sum(isinstance(event, ResponseCompleted) for event in events) == 1


def test_sdk_mock_transport_interleaves_tools_and_orders_completions_by_output_index() -> None:
    """SDK 真实 SSE 解码支持交错工具参数，完成事件仍按 output_index 排序。"""

    async def run() -> list[Any]:
        items = [
            {
                "type": "function_call",
                "id": f"fc_{i}",
                "call_id": f"call_{i}",
                "name": f"tool_{i}",
                "arguments": "{}",
                "status": "completed",
            }
            for i in range(2)
        ]
        payloads = [
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "sequence_number": 0,
                    "output_index": 0,
                    "item": {**items[0], "status": "in_progress"},
                },
            ),
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "sequence_number": 1,
                    "output_index": 1,
                    "item": {**items[1], "status": "in_progress"},
                },
            ),
            (
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "sequence_number": 2,
                    "output_index": 0,
                    "item_id": "fc_0",
                    "delta": "{",
                },
            ),
            (
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "sequence_number": 3,
                    "output_index": 1,
                    "item_id": "fc_1",
                    "delta": "{",
                },
            ),
            (
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "sequence_number": 4,
                    "output_index": 1,
                    "item_id": "fc_1",
                    "delta": "}",
                },
            ),
            (
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "sequence_number": 5,
                    "output_index": 0,
                    "item_id": "fc_0",
                    "delta": "}",
                },
            ),
            (
                "response.function_call_arguments.done",
                {
                    "type": "response.function_call_arguments.done",
                    "sequence_number": 6,
                    "output_index": 0,
                    "item_id": "fc_0",
                    "arguments": "{}",
                },
            ),
            (
                "response.function_call_arguments.done",
                {
                    "type": "response.function_call_arguments.done",
                    "sequence_number": 7,
                    "output_index": 1,
                    "item_id": "fc_1",
                    "arguments": "{}",
                },
            ),
            (
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "sequence_number": 8,
                    "output_index": 1,
                    "item": items[1],
                },
            ),
            (
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "sequence_number": 9,
                    "output_index": 0,
                    "item": items[0],
                },
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "sequence_number": 10,
                    "response": {
                        "id": "resp_tools",
                        "object": "response",
                        "created_at": 1,
                        "status": "completed",
                        "model": "model",
                        "output": items,
                        "parallel_tool_calls": True,
                        "tool_choice": "auto",
                        "tools": [],
                        "usage": None,
                    },
                },
            ),
        ]
        body = b"".join(
            f"event: {name}\ndata: {json.dumps(value)}\n\n".encode() for name, value in payloads
        )

        async def handle(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, content=body, request=request
            )

        cfg = profile("assistant", "secret")
        adapter = OpenAIResponsesAdapter({"assistant": cfg})
        sdk_http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
        sdk = AsyncOpenAI(
            api_key="secret",
            base_url="https://api.openai.com/v1",
            max_retries=0,
            http_client=sdk_http,
        )
        adapter._clients[
            (
                "https://api.openai.com/v1",
                hashlib.sha256(b"secret").hexdigest(),
                cfg.max_response_bytes,
            )
        ] = sdk
        request = LlmRequest(
            "assistant",
            (LlmMessage(LlmRole.USER, "hi"),),
            tools=(
                LlmTool(
                    "tool_0",
                    "",
                    {"type": "object", "properties": {}, "additionalProperties": False},
                ),
                LlmTool(
                    "tool_1",
                    "",
                    {"type": "object", "properties": {}, "additionalProperties": False},
                ),
            ),
        )
        async with adapter.stream("assistant", request) as stream:
            events = [event async for event in stream]
        await adapter.aclose()
        return events

    events = asyncio.run(run())
    completed = [event for event in events if isinstance(event, ToolCallCompleted)]
    assert [event.output_index for event in completed] == [0, 1]


def test_stream_withholds_tool_completion_until_final_response_validation() -> None:
    """末态响应与逐项结果不一致时不得发布已观察到的工具完成事件。"""
    adapter = OpenAIResponsesAdapter({"assistant": profile("assistant", "secret")})
    final_item = {
        "type": "function_call",
        "id": "fc_pending",
        "call_id": "call_pending",
        "name": "lookup",
        "arguments": "{}",
        "status": "completed",
    }
    mismatched_item = {**final_item, "arguments": '{"changed":true}'}
    stream = FakeStream(
        [
            SimpleNamespace(
                type="response.output_item.added",
                output_index=0,
                item={**final_item, "status": "in_progress"},
            ),
            SimpleNamespace(
                type="response.function_call_arguments.done",
                output_index=0,
                item_id="fc_pending",
                arguments="{}",
            ),
            SimpleNamespace(type="response.output_item.done", output_index=0, item=final_item),
            SimpleNamespace(
                type="response.completed",
                response=Response.model_validate(
                    {
                        "id": "resp_pending",
                        "object": "response",
                        "created_at": 1,
                        "status": "completed",
                        "model": "model",
                        "output": [mismatched_item],
                        "parallel_tool_calls": False,
                        "tool_choice": "auto",
                        "tools": [],
                        "usage": None,
                    }
                ),
            ),
        ]
    )

    async def consume() -> list[Any]:
        emitted: list[Any] = []
        with pytest.raises(LlmProtocolError):
            async for event in adapter._events(stream, adapter._profile("assistant")):
                emitted.append(event)
        return emitted

    emitted = asyncio.run(consume())
    assert not any(isinstance(event, ToolCallCompleted) for event in emitted)
    assert not any(isinstance(event, ResponseCompleted) for event in emitted)


def test_sdk_mock_transport_parses_reasoning_summary_lifecycle() -> None:
    """官方 SDK 将 reasoning summary SSE 解码为受校验的可续接输出。"""

    async def run() -> list[Any]:
        final_item = {
            "type": "reasoning",
            "id": "rs_sdk",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": "thought"}],
            "content": [],
            "encrypted_content": "",
        }
        events = [
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "sequence_number": 0,
                    "output_index": 0,
                    "item": {
                        "type": "reasoning",
                        "id": "rs_sdk",
                        "status": "in_progress",
                        "summary": [],
                        "content": [],
                        "encrypted_content": "",
                    },
                },
            ),
            (
                "response.reasoning_summary_part.added",
                {
                    "type": "response.reasoning_summary_part.added",
                    "sequence_number": 1,
                    "output_index": 0,
                    "item_id": "rs_sdk",
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""},
                },
            ),
            (
                "response.reasoning_summary_text.delta",
                {
                    "type": "response.reasoning_summary_text.delta",
                    "sequence_number": 2,
                    "output_index": 0,
                    "item_id": "rs_sdk",
                    "summary_index": 0,
                    "delta": "thought",
                },
            ),
            (
                "response.reasoning_summary_text.done",
                {
                    "type": "response.reasoning_summary_text.done",
                    "sequence_number": 3,
                    "output_index": 0,
                    "item_id": "rs_sdk",
                    "summary_index": 0,
                    "text": "thought",
                },
            ),
            (
                "response.reasoning_summary_part.done",
                {
                    "type": "response.reasoning_summary_part.done",
                    "sequence_number": 4,
                    "output_index": 0,
                    "item_id": "rs_sdk",
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": "thought"},
                },
            ),
            (
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "sequence_number": 5,
                    "output_index": 0,
                    "item": final_item,
                },
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "sequence_number": 6,
                    "response": {
                        "id": "resp_reason",
                        "object": "response",
                        "created_at": 1,
                        "status": "completed",
                        "model": "model",
                        "output": [final_item],
                        "parallel_tool_calls": True,
                        "tool_choice": "auto",
                        "tools": [],
                        "usage": None,
                    },
                },
            ),
        ]
        body = b"".join(
            f"event: {name}\ndata: {json.dumps(value)}\n\n".encode() for name, value in events
        )

        async def handle(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, content=body, request=request
            )

        cfg = profile("assistant", "secret")
        adapter = OpenAIResponsesAdapter({"assistant": cfg})
        sdk_http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
        sdk = AsyncOpenAI(
            api_key="secret",
            base_url="https://api.openai.com/v1",
            max_retries=0,
            http_client=sdk_http,
        )
        adapter._clients[
            (
                "https://api.openai.com/v1",
                hashlib.sha256(b"secret").hexdigest(),
                cfg.max_response_bytes,
            )
        ] = sdk
        request = LlmRequest("assistant", (LlmMessage(LlmRole.USER, "hi"),))
        async with adapter.stream("assistant", request) as stream:
            result = [item async for item in stream]
        await adapter.aclose()
        return result

    events = asyncio.run(run())
    assert len([event for event in events if isinstance(event, ResponseCompleted)]) == 1


def test_sdk_mock_transport_refusal_is_mapped_immediately() -> None:
    """SDK refusal SSE 在缺少 completed 终态前即映射为拒答领域错误。"""

    async def run() -> None:
        payload = {
            "type": "response.refusal.delta",
            "sequence_number": 0,
            "output_index": 0,
            "item_id": "msg_refusal",
            "content_index": 0,
            "delta": "refused",
        }
        body = f"event: response.refusal.delta\ndata: {json.dumps(payload)}\n\n".encode()

        async def handle(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, content=body, request=request
            )

        cfg = profile("assistant", "secret")
        adapter = OpenAIResponsesAdapter({"assistant": cfg})
        sdk_http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
        sdk = AsyncOpenAI(
            api_key="secret",
            base_url="https://api.openai.com/v1",
            max_retries=0,
            http_client=sdk_http,
        )
        adapter._clients[
            (
                "https://api.openai.com/v1",
                hashlib.sha256(b"secret").hexdigest(),
                cfg.max_response_bytes,
            )
        ] = sdk
        request = LlmRequest("assistant", (LlmMessage(LlmRole.USER, "hi"),))
        async with adapter.stream("assistant", request) as stream:
            async for _ in stream:
                pass

    with pytest.raises(LlmRefusalError):
        asyncio.run(run())


def test_sdk_stream_early_exit_closes_http_stream_and_releases_capacity() -> None:
    """SDK SSE 流提前退出会关闭底层传输并归还 Profile 并发名额。"""

    async def run() -> None:
        class TrackedBytes(httpx2.AsyncByteStream):
            def __init__(self, body: bytes) -> None:
                self.body = body
                self.closed = False

            async def __aiter__(self) -> Any:
                yield self.body

            async def aclose(self) -> None:
                self.closed = True

        chunks = [
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "sequence_number": 0,
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "id": "msg_early",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    },
                },
            ),
            (
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "sequence_number": 1,
                    "output_index": 0,
                    "item_id": "msg_early",
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                },
            ),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 2,
                    "output_index": 0,
                    "item_id": "msg_early",
                    "content_index": 0,
                    "delta": "partial",
                    "logprobs": [],
                },
            ),
        ]
        body = b"".join(
            f"event: {name}\ndata: {json.dumps(value)}\n\n".encode() for name, value in chunks
        )
        tracked = TrackedBytes(body)

        async def handle(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=tracked, request=request
            )

        cfg = profile("assistant", "secret")
        adapter = OpenAIResponsesAdapter({"assistant": cfg})
        sdk_http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
        sdk = AsyncOpenAI(
            api_key="secret",
            base_url="https://api.openai.com/v1",
            max_retries=0,
            http_client=sdk_http,
        )
        adapter._clients[
            (
                "https://api.openai.com/v1",
                hashlib.sha256(b"secret").hexdigest(),
                cfg.max_response_bytes,
            )
        ] = sdk
        request = LlmRequest("assistant", (LlmMessage(LlmRole.USER, "hi"),))
        async with adapter.stream("assistant", request) as stream:
            async for event in stream:
                if isinstance(event, TextDelta):
                    break
        assert tracked.closed
        await asyncio.wait_for(adapter._semaphores["assistant"].acquire(), timeout=0.01)
        adapter._semaphores["assistant"].release()
        await adapter.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["missing_item_done", "multiple_terminal"])
def test_sdk_mock_transport_rejects_missing_item_completion_and_multiple_terminals(
    failure: str,
) -> None:
    """真实 SDK SSE 路径拒绝缺失 output_item.done 与重复终态。"""

    async def run() -> None:
        item = {
            "type": "message",
            "id": "msg_terminal",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "ok", "annotations": []}],
        }
        response_payload = {
            "id": "resp_terminal",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "model",
            "output": [item],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": None,
        }
        payloads = [
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "sequence_number": 0,
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "id": "msg_terminal",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    },
                },
            ),
            (
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "sequence_number": 1,
                    "output_index": 0,
                    "item_id": "msg_terminal",
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                },
            ),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 2,
                    "output_index": 0,
                    "item_id": "msg_terminal",
                    "content_index": 0,
                    "delta": "ok",
                    "logprobs": [],
                },
            ),
            (
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "sequence_number": 3,
                    "output_index": 0,
                    "item_id": "msg_terminal",
                    "content_index": 0,
                    "text": "ok",
                    "logprobs": [],
                },
            ),
            (
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "sequence_number": 4,
                    "output_index": 0,
                    "item_id": "msg_terminal",
                    "content_index": 0,
                    "part": item["content"][0],
                },
            ),
        ]
        if failure != "missing_item_done":
            payloads.append(
                (
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "sequence_number": 5,
                        "output_index": 0,
                        "item": item,
                    },
                )
            )
        payloads.append(
            (
                "response.completed",
                {"type": "response.completed", "sequence_number": 6, "response": response_payload},
            )
        )
        if failure == "multiple_terminal":
            payloads.append(
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "sequence_number": 7,
                        "response": response_payload,
                    },
                )
            )
        body = b"".join(
            f"event: {name}\ndata: {json.dumps(value)}\n\n".encode() for name, value in payloads
        )

        async def handle(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, content=body, request=request
            )

        cfg = profile("assistant", "secret")
        adapter = OpenAIResponsesAdapter({"assistant": cfg})
        sdk_http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
        sdk = AsyncOpenAI(
            api_key="secret",
            base_url="https://api.openai.com/v1",
            max_retries=0,
            http_client=sdk_http,
        )
        adapter._clients[
            (
                "https://api.openai.com/v1",
                hashlib.sha256(b"secret").hexdigest(),
                cfg.max_response_bytes,
            )
        ] = sdk
        request = LlmRequest("assistant", (LlmMessage(LlmRole.USER, "hi"),))
        async with adapter.stream("assistant", request) as stream:
            _ = [event async for event in stream]

    with pytest.raises(LlmProtocolError):
        asyncio.run(run())


@pytest.mark.parametrize(
    ("following_event", "error_code"),
    [
        (None, "llm_terminal_event_missing"),
        ("completed", "llm_multiple_terminal_events"),
        ("created", "llm_multiple_terminal_events"),
    ],
)
def test_sdk_mock_transport_empty_completed_reserves_terminal_slot(
    following_event: str | None,
    error_code: str,
) -> None:
    """真实 SDK SSE 中空 completed 也占用唯一终态槽位。"""

    async def run() -> None:
        response_payload = {
            "id": "resp_after_empty",
            "object": "response",
            "created_at": 1,
            "status": "in_progress" if following_event == "created" else "completed",
            "model": "model",
            "output": [],
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
            "usage": None,
        }
        payloads = [
            (
                "response.completed",
                {"type": "response.completed", "sequence_number": 0, "response": None},
            )
        ]
        if following_event == "completed":
            payloads.append(
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "sequence_number": 1,
                        "response": response_payload,
                    },
                )
            )
        elif following_event == "created":
            payloads.append(
                (
                    "response.created",
                    {
                        "type": "response.created",
                        "sequence_number": 1,
                        "response": response_payload,
                    },
                )
            )
        body = b"".join(
            f"event: {name}\ndata: {json.dumps(value)}\n\n".encode() for name, value in payloads
        )

        async def handle(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body,
                request=request,
            )

        cfg = profile("assistant", "secret")
        adapter = OpenAIResponsesAdapter({"assistant": cfg})
        sdk_http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
        sdk = AsyncOpenAI(
            api_key="secret",
            base_url="https://api.openai.com/v1",
            max_retries=0,
            http_client=sdk_http,
        )
        adapter._clients[
            (
                "https://api.openai.com/v1",
                hashlib.sha256(b"secret").hexdigest(),
                cfg.max_response_bytes,
            )
        ] = sdk
        request = LlmRequest("assistant", (LlmMessage(LlmRole.USER, "hi"),))
        try:
            async with adapter.stream("assistant", request) as stream:
                _ = [event async for event in stream]
        finally:
            await adapter.aclose()

    with pytest.raises(LlmProtocolError, match=error_code):
        asyncio.run(run())


def test_sdk_mock_transport_rejects_function_arguments_done_conflict() -> None:
    """SDK function arguments done 必须与累计参数分片完全相同。"""

    async def run() -> None:
        events = [
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "sequence_number": 0,
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "id": "fc_conflict",
                        "call_id": "call_conflict",
                        "name": "lookup",
                        "arguments": "",
                        "status": "in_progress",
                    },
                },
            ),
            (
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "sequence_number": 1,
                    "output_index": 0,
                    "item_id": "fc_conflict",
                    "delta": "{}",
                },
            ),
            (
                "response.function_call_arguments.done",
                {
                    "type": "response.function_call_arguments.done",
                    "sequence_number": 2,
                    "output_index": 0,
                    "item_id": "fc_conflict",
                    "arguments": '{"other":true}',
                },
            ),
        ]
        body = b"".join(
            f"event: {name}\ndata: {json.dumps(value)}\n\n".encode() for name, value in events
        )

        async def handle(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, content=body, request=request
            )

        cfg = profile("assistant", "secret")
        adapter = OpenAIResponsesAdapter({"assistant": cfg})
        sdk_http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
        sdk = AsyncOpenAI(
            api_key="secret",
            base_url="https://api.openai.com/v1",
            max_retries=0,
            http_client=sdk_http,
        )
        adapter._clients[
            (
                "https://api.openai.com/v1",
                hashlib.sha256(b"secret").hexdigest(),
                cfg.max_response_bytes,
            )
        ] = sdk
        tool = LlmTool(
            "lookup", "", {"type": "object", "properties": {}, "additionalProperties": False}
        )
        request = LlmRequest("assistant", (LlmMessage(LlmRole.USER, "hi"),), tools=(tool,))
        async with adapter.stream("assistant", request) as stream:
            _ = [event async for event in stream]

    with pytest.raises(LlmProtocolError):
        asyncio.run(run())


def test_stream_without_terminal_event_fails() -> None:
    """上游断流不能伪造正常完成。"""
    adapter = OpenAIResponsesAdapter({"assistant": profile("assistant", "secret")})
    stream = FakeStream([])

    async def run() -> None:
        async for _event in adapter._events(stream, adapter._profile("assistant")):
            pass

    with pytest.raises(LlmProtocolError):
        asyncio.run(run())


@pytest.mark.parametrize("duplicate_part_done", [False, True])
def test_stream_rejects_conflicting_or_duplicate_message_part_completion(
    duplicate_part_done: bool,
) -> None:
    """累计文本、part done 与 output item done 必须一致且只能完成一次。"""
    adapter = OpenAIResponsesAdapter({"assistant": profile("assistant", "secret")})
    added = ResponseOutputMessage.model_validate(
        {
            "type": "message",
            "id": "msg_x",
            "role": "assistant",
            "status": "in_progress",
            "content": [],
        }
    )
    final_item = ResponseOutputMessage.model_validate(
        {
            "type": "message",
            "id": "msg_x",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "B", "annotations": []}],
        }
    )
    events = [
        SimpleNamespace(type="response.output_item.added", output_index=0, item=added),
        SimpleNamespace(
            type="response.content_part.added",
            output_index=0,
            item_id="msg_x",
            content_index=0,
            part={"type": "output_text", "text": ""},
        ),
        SimpleNamespace(
            type="response.output_text.delta",
            output_index=0,
            item_id="msg_x",
            content_index=0,
            delta="A",
        ),
        SimpleNamespace(
            type="response.output_text.done",
            output_index=0,
            item_id="msg_x",
            content_index=0,
            text="A",
        ),
        SimpleNamespace(
            type="response.content_part.done",
            output_index=0,
            item_id="msg_x",
            content_index=0,
            part={"type": "output_text", "text": "A"},
        ),
    ]
    if duplicate_part_done:
        events.append(
            SimpleNamespace(
                type="response.content_part.done",
                output_index=0,
                item_id="msg_x",
                content_index=0,
                part={"type": "output_text", "text": "A"},
            )
        )
    else:
        events.append(
            SimpleNamespace(type="response.output_item.done", output_index=0, item=final_item)
        )
    stream = FakeStream(events)

    async def consume() -> None:
        async for _ in adapter._events(stream, adapter._profile("assistant")):
            pass

    with pytest.raises(LlmProtocolError):
        asyncio.run(consume())


def test_bounded_transport_stream_rejects_single_oversize_sse_event_and_body() -> None:
    """SSE 单事件与累计原始字节在解析前受限。"""

    class ByteStream(httpx2.AsyncByteStream):
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks

        async def __aiter__(self) -> Any:
            for chunk in self.chunks:
                yield chunk

        async def aclose(self) -> None:
            return None

    async def consume(stream: _BoundedResponseStream) -> None:
        async for _ in stream:
            pass

    with pytest.raises(ResponseLimitError, match="llm_response_too_large"):
        asyncio.run(
            consume(
                _BoundedResponseStream(ByteStream([b"x" * 40_000, b"y" * 30_000]), 65_000, True)
            )
        )
    asyncio.run(
        consume(
            _BoundedResponseStream(ByteStream([b"data: " + b"x" * 70_000 + b"\n\n"]), 200_000, True)
        )
    )
    with pytest.raises(ResponseLimitError, match="llm_response_too_large"):
        asyncio.run(consume(_BoundedResponseStream(ByteStream([b"x" * 40, b"y" * 40]), 64, False)))


def test_sse_line_scanning_is_independent_of_chunking_and_line_endings() -> None:
    """LF/CRLF/CR 事件以任意字节边界分块时均完整通过。"""

    class ByteStream(httpx2.AsyncByteStream):
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks

        async def __aiter__(self) -> Any:
            for chunk in self.chunks:
                yield chunk

        async def aclose(self) -> None:
            return None

    async def collect(chunks: list[bytes]) -> bytes:
        stream = _BoundedResponseStream(ByteStream(chunks), 1024, True)
        return b"".join([chunk async for chunk in stream])

    lf = b"event: response.completed\ndata: {}\n\n"
    crlf = b"event: response.completed\r\ndata: {}\r\n\r\n"
    cr = b"event: response.completed\rdata: {}\r\r"
    assert asyncio.run(collect([lf])) == lf
    assert asyncio.run(collect([bytes([byte]) for byte in lf])) == lf
    assert asyncio.run(collect([crlf[:20], crlf[20:]])) == crlf
    assert asyncio.run(collect([cr])) == cr


def test_stream_incomplete_fails() -> None:
    """incomplete 终态传播领域未完成错误。"""
    adapter = OpenAIResponsesAdapter({"assistant": profile("assistant", "secret")})
    incomplete = SimpleNamespace(type="response.incomplete", response=response())
    stream = FakeStream([incomplete])

    async def run() -> None:
        async for _event in adapter._events(stream, adapter._profile("assistant")):
            pass

    with pytest.raises(LlmIncompleteError):
        asyncio.run(run())


def test_stream_early_exit_closes_upstream_and_releases_profile_capacity() -> None:
    """调用方提前退出流上下文时关闭 SDK 流并归还并发名额。"""

    async def run() -> None:
        adapter = OpenAIResponsesAdapter({"assistant": profile("assistant", "secret")})
        output_item = ResponseOutputMessage.model_validate(
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            }
        )
        content_part = {"type": "output_text", "text": ""}
        text_delta = SimpleNamespace(
            type="response.output_text.delta",
            output_index=0,
            item_id="msg_1",
            content_index=0,
            delta="hi",
        )
        upstream = FakeStream(
            [
                SimpleNamespace(
                    type="response.output_item.added", output_index=0, item=output_item
                ),
                SimpleNamespace(
                    type="response.content_part.added",
                    output_index=0,
                    item_id="msg_1",
                    content_index=0,
                    part=content_part,
                ),
                text_delta,
            ]
        )

        class ResponsesStub:
            async def create(self, **_kwargs: Any) -> FakeStream:
                return upstream

        class ClientStub:
            responses = ResponsesStub()

            async def close(self) -> None:
                return None

        key = (
            "https://api.openai.com/v1",
            hashlib.sha256(b"secret").hexdigest(),
            adapter._profile("assistant").max_response_bytes,
        )
        adapter._clients[key] = ClientStub()  # type: ignore[assignment]
        request = LlmRequest("assistant", (LlmMessage(LlmRole.USER, "hello"),))
        async with adapter.stream("assistant", request) as events:
            async for _event in events:
                break
        assert upstream.closed
        await asyncio.wait_for(adapter._semaphores["assistant"].acquire(), timeout=0.01)
        adapter._semaphores["assistant"].release()
        await adapter.aclose()

    asyncio.run(run())
