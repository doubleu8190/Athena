"""LLM 请求校验与 Profile 路由。"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from app.llm.config import LlmProfile
from domain.llm import LlmProvider
from domain.llm.model import (
    LlmCapabilityError,
    LlmRequest,
    LlmRequestError,
    LlmResponse,
    LlmStreamEvent,
    LlmStructuredJsonError,
    LlmStructuredOutputError,
    LlmStructuredValidationError,
    LlmText,
    LlmToolCall,
    StructuredLlmRequest,
    StructuredLlmResponse,
)


def _freeze(value: Any) -> Any:
    """深度复制并冻结 JSON 样式数据，隔离调用方后续修改。"""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """把不可变 JSON 快照转回可序列化容器。"""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _reject_remote_refs(schema: Any) -> None:
    """仅允许本地片段引用，避免 Schema 验证器访问网络。"""
    if isinstance(schema, Mapping):
        ref = schema.get("$ref")
        if ref is not None and (not isinstance(ref, str) or not ref.startswith("#/")):
            raise LlmRequestError()
        for value in schema.values():
            _reject_remote_refs(value)
    elif isinstance(schema, (list, tuple)):
        for value in schema:
            _reject_remote_refs(value)


def _validate_continuation_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """校验可供 Responses API 重放的 function call、reasoning 和结果项。"""
    if not isinstance(item, Mapping):
        raise LlmRequestError("llm_continuation_invalid")
    plain = _thaw(copy.deepcopy(dict(item)))
    try:
        encoded = json.dumps(plain, ensure_ascii=False, allow_nan=False)
        parsed = json.loads(encoded)
    except (TypeError, ValueError, UnicodeError):
        raise LlmRequestError("llm_continuation_invalid") from None
    if not isinstance(parsed, dict):
        raise LlmRequestError("llm_continuation_invalid")
    kind = parsed.get("type")
    allowed_fields = {
        "function_call": {"type", "id", "call_id", "name", "arguments", "status"},
        "function_call_output": {"type", "call_id", "output"},
        "reasoning": {"type", "id", "summary", "content", "encrypted_content", "status"},
        "message": {"type", "id", "role", "status", "content", "phase"},
    }
    if kind not in allowed_fields or set(parsed) - allowed_fields[kind]:
        raise LlmRequestError("llm_continuation_invalid")
    if kind in {"function_call", "function_call_output"}:
        if not all(isinstance(parsed.get(key), str) and parsed[key] for key in ("call_id",)):
            raise LlmRequestError("llm_continuation_invalid")
    if kind == "function_call":
        if not all(
            isinstance(parsed.get(key), str) and parsed[key] for key in ("id", "name", "arguments")
        ):
            raise LlmRequestError("llm_continuation_invalid")
        try:
            arguments = json.loads(parsed["arguments"])
        except (TypeError, ValueError):
            raise LlmRequestError("llm_continuation_invalid") from None
        if not isinstance(arguments, dict):
            raise LlmRequestError("llm_continuation_invalid")
        if parsed.get("status", "completed") != "completed":
            raise LlmRequestError("llm_continuation_invalid")
    elif kind == "function_call_output":
        if not isinstance(parsed.get("output"), str):
            raise LlmRequestError("llm_continuation_invalid")
    else:
        if not isinstance(parsed.get("id"), str) or not parsed["id"]:
            raise LlmRequestError("llm_continuation_invalid")
        if not isinstance(parsed.get("encrypted_content", ""), str):
            raise LlmRequestError("llm_continuation_invalid")
        if kind == "message":
            if (
                parsed.get("role") != "assistant"
                or parsed.get("status") != "completed"
                or parsed.get("phase") not in (None, "commentary", "final_answer")
            ):
                raise LlmRequestError("llm_continuation_invalid")
            content = parsed.get("content")
            if not isinstance(content, list):
                raise LlmRequestError("llm_continuation_invalid")
            for part in content:
                if (
                    not isinstance(part, dict)
                    or part.get("type") != "output_text"
                    or not isinstance(part.get("text"), str)
                    or set(part) - {"type", "text", "annotations"}
                ):
                    raise LlmRequestError("llm_continuation_invalid")
                annotations = part.get("annotations", [])
                if not isinstance(annotations, list):
                    raise LlmRequestError("llm_continuation_invalid")
                for annotation in annotations:
                    if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
                        raise LlmRequestError("llm_continuation_invalid")
                    if set(annotation) - {"type", "start_index", "end_index", "url", "title"}:
                        raise LlmRequestError("llm_continuation_invalid")
                    if (
                        type(annotation.get("start_index")) is not int
                        or type(annotation.get("end_index")) is not int
                        or annotation["start_index"] < 0
                        or annotation["end_index"] < annotation["start_index"]
                        or annotation["end_index"] > len(part["text"])
                        or not isinstance(annotation.get("url"), str)
                        or not isinstance(annotation.get("title"), str)
                    ):
                        raise LlmRequestError("llm_continuation_invalid")
            return parsed
        for field_name in ("summary", "content"):
            value = parsed.get(field_name, [])
            expected_type = "summary_text" if field_name == "summary" else "reasoning_text"
            if not isinstance(value, list) or any(
                not isinstance(entry, dict)
                or set(entry) != {"type", "text"}
                or entry.get("type") != expected_type
                or not isinstance(entry.get("text"), str)
                for entry in value
            ):
                raise LlmRequestError("llm_continuation_invalid")
        if parsed.get("status", "completed") != "completed":
            raise LlmRequestError("llm_continuation_invalid")
    return parsed


class LlmService:
    """执行请求预检、部署限制和结构化输出验证。"""

    def __init__(self, provider: LlmProvider, profiles: Mapping[str, LlmProfile]) -> None:
        """创建服务。"""
        self._provider = provider
        self._profiles = dict(profiles)

    async def complete(self, request: LlmRequest) -> LlmResponse:
        """校验后执行一次非流式调用。"""
        profile = self._prepare(request, "tools" if request.tools else None)
        return await self._provider.complete(profile.name, self._snapshot(request, profile))

    def stream(
        self, request: LlmRequest
    ) -> AbstractAsyncContextManager[AsyncIterator[LlmStreamEvent]]:
        """创建持有供应商资源的流式异步上下文。"""
        profile = self._prepare(request, "stream")
        if request.tools and "tools" not in profile.capabilities:
            raise LlmCapabilityError()
        return self._provider.stream(
            profile.name,
            self._snapshot(request, profile, default_timeout=profile.stream_timeout_seconds),
        )

    async def generate_structured(self, request: StructuredLlmRequest) -> StructuredLlmResponse:
        """使用 JSON Schema 生成并验证一个 Pydantic 对象。"""
        base = request.request
        if base.tools:
            raise LlmRequestError()
        profile = self._prepare(base, "structured")
        if not hasattr(request.output_model, "model_json_schema") or not hasattr(
            request.output_model, "model_validate"
        ):
            raise LlmRequestError()
        try:
            schema = request.output_model.model_json_schema()
            Draft202012Validator.check_schema(schema)
            _reject_remote_refs(schema)
            self._validate_strict_schema(schema)
        except Exception:
            raise LlmStructuredOutputError("llm_structured_schema_invalid") from None
        response = await self._provider.generate_structured(
            profile.name, self._snapshot(base, profile), request.schema_name, schema
        )
        text_parts: list[str] = []
        for item in response.output:
            if isinstance(item, LlmText):
                text_parts.append(item.text)
            else:
                raise LlmRequestError("llm_structured_tool_output_unsupported")
        try:
            value = json.loads("".join(text_parts))
        except (TypeError, ValueError):
            raise LlmStructuredJsonError() from None
        try:
            parsed = request.output_model.model_validate(value)
        except Exception:
            raise LlmStructuredValidationError() from None
        return StructuredLlmResponse(parsed, response)

    def _prepare(self, request: LlmRequest, capability: str | None) -> LlmProfile:
        """验证 Profile 能力、请求上限及每个绑定工具的 Schema。"""
        profile = self._profiles.get(request.model_profile)
        if profile is None:
            raise LlmRequestError("llm_profile_unknown")
        if capability is not None and capability not in profile.capabilities:
            raise LlmCapabilityError()
        if request.temperature is not None and "temperature" not in profile.capabilities:
            raise LlmCapabilityError()
        if (
            request.max_output_tokens is not None
            and not 1 <= request.max_output_tokens <= profile.max_output_tokens
        ):
            raise LlmRequestError()
        timeout_limit = (
            profile.stream_timeout_seconds
            if capability == "stream"
            else profile.request_timeout_seconds
        )
        if request.timeout_seconds is not None and not 0 < request.timeout_seconds <= timeout_limit:
            raise LlmRequestError()
        if len(request.tools) > profile.max_tools:
            raise LlmRequestError()
        names: set[str] = set()
        try:
            for message in request.messages:
                for item in message.continuation_items:
                    _validate_continuation_item(item)
            for tool in request.tools:
                if not tool.name or len(tool.name) > 64 or tool.name in names:
                    raise ValueError
                names.add(tool.name)
                schema = copy.deepcopy(dict(tool.input_schema))
                Draft202012Validator.check_schema(schema)
                _reject_remote_refs(schema)
                if schema.get("type", "object") != "object":
                    raise ValueError
            if request.tool_choice not in {"auto", "none", "required"}:
                if (
                    not request.tool_choice.startswith("tool:")
                    or request.tool_choice[5:] not in names
                ):
                    raise ValueError
        except Exception:
            raise LlmRequestError() from None
        try:
            size = len(
                json.dumps(
                    {
                        "messages": [
                            {
                                "role": message.role.value,
                                "content": message.content,
                                "tool_call_id": message.tool_call_id,
                                "name": message.name,
                                "tool_calls": [
                                    {
                                        "call_id": call.call_id,
                                        "name": call.name,
                                        "arguments": _thaw(call.arguments),
                                    }
                                    for call in message.tool_calls
                                ],
                                "continuation_items": [
                                    _thaw(item) for item in message.continuation_items
                                ],
                            }
                            for message in request.messages
                        ],
                        "tools": [
                            {
                                "name": tool.name,
                                "description": tool.description,
                                "schema": _thaw(tool.input_schema),
                            }
                            for tool in request.tools
                        ],
                    },
                    ensure_ascii=False,
                ).encode()
            )
        except (TypeError, ValueError, UnicodeError):
            raise LlmRequestError() from None
        if size > profile.max_input_bytes:
            raise LlmRequestError("llm_input_too_large")
        if request.tool_choice == "required" and not request.tools:
            raise LlmRequestError()
        return profile

    @staticmethod
    def _snapshot(
        request: LlmRequest, profile: LlmProfile, *, default_timeout: float | None = None
    ) -> LlmRequest:
        """构造不可变工具 Schema 快照并约束请求级上限。"""
        tools = tuple(
            type(tool)(tool.name, tool.description, _freeze(copy.deepcopy(dict(tool.input_schema))))
            for tool in request.tools
        )
        return type(request)(
            model_profile=request.model_profile,
            messages=tuple(
                type(message)(
                    role=message.role,
                    content=message.content,
                    tool_call_id=message.tool_call_id,
                    name=message.name,
                    tool_calls=tuple(
                        LlmToolCall(
                            call_id=call.call_id,
                            name=call.name,
                            arguments=_freeze(copy.deepcopy(dict(call.arguments))),
                        )
                        for call in message.tool_calls
                    ),
                    continuation_items=tuple(
                        _freeze(_validate_continuation_item(item))
                        for item in message.continuation_items
                    ),
                )
                for message in request.messages
            ),
            tools=tools,
            tool_choice=request.tool_choice,
            max_output_tokens=request.max_output_tokens or profile.max_output_tokens,
            temperature=request.temperature,
            timeout_seconds=request.timeout_seconds
            or default_timeout
            or profile.request_timeout_seconds,
        )

    @staticmethod
    def _validate_strict_schema(schema: Mapping[str, Any]) -> None:
        """拒绝常见的严格输出 Schema 不支持结构，不重写业务 Schema。"""
        if schema.get("type") == "object" and schema.get("additionalProperties") is not False:
            raise ValueError
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        if (
            schema.get("type") == "object"
            and isinstance(properties, Mapping)
            and set(required) != set(properties)
        ):
            raise ValueError
        for value in schema.values():
            if isinstance(value, Mapping):
                LlmService._validate_strict_schema(value)
            elif isinstance(value, list):
                for nested in value:
                    if isinstance(nested, Mapping):
                        LlmService._validate_strict_schema(nested)
