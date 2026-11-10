"""LLM 配置和应用层请求边界测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict

from adapter.llm.openai_chat_completions import OpenAIChatCompletionsAdapter
from adapter.llm.openai_responses import OpenAIResponsesAdapter
from app.llm.config import LlmProfile
from app.llm.service import LlmService
from domain.llm.model import (
    LlmMessage,
    LlmRequest,
    LlmRequestError,
    LlmRole,
    LlmStructuredJsonError,
    LlmStructuredValidationError,
    LlmText,
    LlmTool,
    LlmToolCall,
    StructuredLlmRequest,
)


class ProviderStub:
    """记录供应商调用的轻量端口替身。"""

    def __init__(self) -> None:
        self.request: LlmRequest | None = None
        self.schema: dict[str, Any] | None = None
        self.structured_output = '{"answer":"ok"}'

    async def complete(self, profile: str, request: LlmRequest) -> Any:
        self.request = request
        return None

    def stream(self, profile: str, request: LlmRequest) -> Any:
        self.request = request
        return None

    async def generate_structured(
        self, profile: str, request: LlmRequest, schema_name: str, schema: Any
    ) -> Any:
        self.request = request
        self.schema = schema
        from domain.llm.model import LlmResponse

        return LlmResponse(
            (LlmText(self.structured_output),), "completed", None, "model", "id", None, None
        )


def make_profile(
    capabilities: frozenset[str] = frozenset({"tools", "stream", "structured"}),
) -> LlmProfile:
    """构造不依赖环境的 Profile。"""
    return LlmProfile("assistant", "key", "model", None, capabilities)


def test_runtime_profile_repr_hides_api_key() -> None:
    """LLM 配置对象的日志展示不包含供应商密钥。"""
    assert "key" not in repr(make_profile())


def test_tool_schema_is_copied_and_frozen_at_call_boundary() -> None:
    """工具嵌套 Schema 不能在调用后被调用方改变。"""
    provider = ProviderStub()
    service = LlmService(provider, {"assistant": make_profile()})
    schema: dict[str, Any] = {"type": "object", "properties": {"value": {"type": "string"}}}
    request = LlmRequest(
        "assistant", (LlmMessage(LlmRole.USER, "hello"),), (LlmTool("tool", "desc", schema),)
    )
    copied = service._snapshot(request, make_profile())
    schema["properties"]["value"]["type"] = "integer"
    assert copied.tools[0].input_schema["properties"]["value"]["type"] == "string"
    with pytest.raises(TypeError):
        copied.tools[0].input_schema["properties"]["new"] = {}


def test_public_service_snapshots_typed_tool_calls_and_results() -> None:
    """公开 stream 保留 assistant 调用及 tool 结果，并冻结参数快照。"""
    provider = ProviderStub()
    service = LlmService(provider, {"assistant": make_profile()})
    arguments: dict[str, Any] = {"nested": {"answer": "A"}}
    request = LlmRequest(
        "assistant",
        (
            LlmMessage(
                LlmRole.ASSISTANT,
                tool_calls=(LlmToolCall("call_1", "request_user_input", arguments),),
            ),
            LlmMessage(LlmRole.TOOL, '{"confirmed":true}', tool_call_id="call_1"),
        ),
    )
    service.stream(request)
    assert provider.request is not None
    serialized = OpenAIChatCompletionsAdapter._messages(provider.request.messages)
    assert serialized[0]["tool_calls"][0]["id"] == "call_1"
    assert serialized[0]["tool_calls"][0]["function"]["arguments"] == ('{"nested":{"answer":"A"}}')
    assert serialized[1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"confirmed":true}',
    }
    arguments["nested"]["answer"] = "mutated"
    assert provider.request.messages[0].tool_calls[0].arguments["nested"]["answer"] == "A"
    with pytest.raises(TypeError):
        provider.request.messages[0].tool_calls[0].arguments["nested"]["new"] = "blocked"


def test_tool_call_arguments_count_towards_input_byte_limit() -> None:
    """工具调用参数计入 LlmService 的输入字节预算。"""
    profile = replace(make_profile(), max_input_bytes=180)
    service = LlmService(ProviderStub(), {"assistant": profile})
    request = LlmRequest(
        "assistant",
        (
            LlmMessage(
                LlmRole.ASSISTANT,
                tool_calls=(LlmToolCall("call_1", "lookup", {"payload": "x" * 512}),),
            ),
            LlmMessage(LlmRole.TOOL, "done", tool_call_id="call_1"),
        ),
    )
    with pytest.raises(LlmRequestError, match="llm_input_too_large"):
        service.stream(request)


def test_public_service_sends_nested_tool_call_arguments_to_responses() -> None:
    """公开 LlmService 经真实 Responses SDK 发送冻结的嵌套工具参数。"""

    async def run() -> None:
        calls: list[dict[str, Any]] = []

        async def handle(request: httpx2.Request) -> httpx2.Response:
            calls.append(json.loads(request.content))
            return httpx2.Response(
                200,
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

        profile = make_profile()
        adapter = OpenAIResponsesAdapter({"assistant": profile})
        sdk_http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
        sdk = AsyncOpenAI(
            api_key=profile.api_key,
            base_url="https://api.openai.com/v1",
            max_retries=0,
            http_client=sdk_http,
        )
        adapter._clients[
            (
                "https://api.openai.com/v1",
                hashlib.sha256(profile.api_key.encode()).hexdigest(),
                profile.max_response_bytes,
            )
        ] = sdk
        service = LlmService(adapter, {"assistant": profile})
        arguments: dict[str, Any] = {
            "filter": {"status": "open"},
            "items": [{"id": 1}, {"id": 2}],
        }
        request = LlmRequest(
            "assistant",
            (
                LlmMessage(
                    LlmRole.ASSISTANT,
                    tool_calls=(LlmToolCall("call_1", "lookup", arguments),),
                ),
                LlmMessage(LlmRole.TOOL, "found", tool_call_id="call_1"),
            ),
        )
        await service.complete(request)
        assert calls[0]["input"] == [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": '{"filter":{"status":"open"},"items":[{"id":1},{"id":2}]}',
            },
            {"type": "function_call_output", "call_id": "call_1", "output": "found"},
        ]
        await adapter.aclose()

    asyncio.run(run())


def test_external_schema_reference_is_rejected_without_resolution() -> None:
    """工具校验拒绝所有非本地引用，不触发网络读取。"""
    provider = ProviderStub()
    service = LlmService(provider, {"assistant": make_profile()})
    request = LlmRequest(
        "assistant",
        (LlmMessage(LlmRole.USER, "hello"),),
        (LlmTool("tool", "desc", {"type": "object", "$ref": "https://example.invalid/schema"}),),
    )
    with pytest.raises(LlmRequestError):
        service._prepare(request, "tools")


class Answer(BaseModel):
    """测试结构化输出模型。"""

    model_config = ConfigDict(extra="forbid")
    answer: str


def test_structured_output_is_parsed_into_pydantic_model() -> None:
    """结构化响应经 JSON 和 Pydantic 校验后返回实例。"""
    provider = ProviderStub()
    service = LlmService(provider, {"assistant": make_profile(frozenset({"structured"}))})
    request = LlmRequest("assistant", (LlmMessage(LlmRole.USER, "hello"),))
    result = asyncio.run(service.generate_structured(StructuredLlmRequest(request, Answer)))
    assert isinstance(result.value, Answer)
    assert result.value.answer == "ok"


@pytest.mark.parametrize(
    "output,error_type",
    [
        ("not-json", LlmStructuredJsonError),
        ('{"answer": 3}', LlmStructuredValidationError),
    ],
)
def test_structured_errors_distinguish_json_from_validation(
    output: str, error_type: type[Exception]
) -> None:
    provider = ProviderStub()
    provider.structured_output = output
    service = LlmService(provider, {"assistant": make_profile(frozenset({"structured"}))})
    request = LlmRequest("assistant", (LlmMessage(LlmRole.USER, "hello"),))
    with pytest.raises(error_type):
        asyncio.run(service.generate_structured(StructuredLlmRequest(request, Answer)))


def test_structured_schema_is_rejected_instead_of_silently_rewritten() -> None:
    """不兼容 strict JSON Schema 在调用供应商前失败。"""

    class OptionalAnswer(BaseModel):
        answer: str | None = None

    service = LlmService(ProviderStub(), {"assistant": make_profile(frozenset({"structured"}))})
    request = LlmRequest("assistant", (LlmMessage(LlmRole.USER, "hello"),))
    with pytest.raises(Exception, match="llm_structured_schema_invalid"):
        asyncio.run(service.generate_structured(StructuredLlmRequest(request, OptionalAnswer)))


def test_message_continuation_round_trips_through_service_into_adapter_input() -> None:
    """完成的 assistant message 输出可经服务校验并原样续接到 Responses input。"""
    profile = make_profile()
    service = LlmService(ProviderStub(), {"assistant": profile})
    prior_item = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "hello", "annotations": []}],
    }
    next_request = LlmRequest(
        "assistant",
        (LlmMessage(LlmRole.ASSISTANT, continuation_items=(prior_item,)),),
    )
    snapshot = service._snapshot(next_request, profile)
    assert OpenAIResponsesAdapter._input(snapshot.messages) == [prior_item]


def test_continuation_items_preserve_reasoning_and_tool_call_order() -> None:
    """reasoning、function call、tool output 的续接顺序和 call_id 保持不变。"""
    profile = make_profile()
    service = LlmService(ProviderStub(), {"assistant": profile})
    items = (
        {"type": "reasoning", "id": "rs_1", "summary": [], "content": [], "encrypted_content": ""},
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": "{}",
        },
    )
    request = LlmRequest(
        "assistant",
        (
            LlmMessage(LlmRole.ASSISTANT, continuation_items=items),
            LlmMessage(
                LlmRole.TOOL,
                "",
                tool_call_id="call_1",
                continuation_items=(
                    {"type": "function_call_output", "call_id": "call_1", "output": "done"},
                ),
            ),
        ),
    )
    snapshot = service._snapshot(request, profile)
    assert OpenAIResponsesAdapter._input(snapshot.messages) == [
        *items,
        {"type": "function_call_output", "call_id": "call_1", "output": "done"},
    ]
