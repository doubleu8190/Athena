"""LLM 配置和应用层请求边界测试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from adapter.llm.openai_responses import OpenAIResponsesAdapter
from app.llm.config import LlmProfile, parse_profiles
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


def test_parse_profiles_rejects_duplicate_and_missing_credentials() -> None:
    """重复 JSON 字段和缺失 API Key 都只暴露固定错误码。"""
    with pytest.raises(Exception, match="llm_configuration_invalid"):
        parse_profiles('{"x":{"model":"m","model":"n"}}', {})
    config = json.dumps({"x": {"model": "m", "api_key_env": "OPENAI_API_KEY", "capabilities": {}}})
    with pytest.raises(Exception, match="llm_configuration_invalid"):
        parse_profiles(config, {})


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_concurrency", 0.5),
        ("max_concurrency", True),
        ("max_response_bytes", 0),
        ("max_tools", -1),
        ("max_output_tokens", 1.2),
        ("request_timeout_seconds", float("nan")),
        ("stream_timeout_seconds", float("inf")),
    ],
)
def test_parse_profiles_rejects_invalid_numeric_limits(field: str, value: Any) -> None:
    config = {"x": {"model": "m", "api_key_env": "KEY", "capabilities": {}, field: value}}
    with pytest.raises(Exception, match="llm_configuration_invalid"):
        parse_profiles(json.dumps(config), {"KEY": "fake"})


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
