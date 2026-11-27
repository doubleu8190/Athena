"""Run 使用的模型配置版本及其局部更新与领域不变量校验。"""

from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from common.unset import UNSET, UnsetType
from domain.llm.model.profile_errors import ModelProfileError
from domain.mcp import MAX_MCP_SECRET_BYTES

# 上下文窗口与单次输出有各自的上限，扩大上下文容量不改变输出保护边界。
MAX_CONTEXT_WINDOW_TOKENS = 1_048_576
MAX_OUTPUT_TOKENS = 1_000_000
MAX_SCENARIO_LABEL_LENGTH = 64
MAX_SCENARIO_DESCRIPTION_LENGTH = 255


@dataclass(frozen=True, slots=True)
class ModelProfileVersion:
    """不含凭据明文的模型配置投影。"""

    id: str
    name: str
    protocol: Literal["chat_completions"]
    base_url: str
    model: str
    context_window_tokens: int
    tokenizer_encoding: Literal["cl100k_base", "o200k_base"]
    supports_stream: bool
    supports_tools: bool
    max_output_tokens: int
    request_timeout_seconds: int
    execution_timeout_seconds: int
    max_concurrency: int
    temperature: float | None
    enabled: bool
    created_at: datetime
    updated_at: datetime
    supports_parallel_tool_calls: bool = False
    visible_to_users: bool = True
    scenario_label: str | None = None
    scenario_description: str | None = None
    supports_vision: bool = False
    version: int = 1


@dataclass(frozen=True, slots=True)
class RuntimeModelProfile:
    """调用时解析凭据的内部模型配置。"""

    version: ModelProfileVersion
    api_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ModelProfileInput:
    """管理员提交的新模型版本字段。"""

    name: str
    base_url: str
    model: str
    api_key: str = field(repr=False)
    context_window_tokens: int
    max_output_tokens: int
    tokenizer_encoding: Literal["cl100k_base", "o200k_base"] = "o200k_base"
    supports_stream: bool = True
    supports_tools: bool = True
    request_timeout_seconds: int = 120
    execution_timeout_seconds: int = 300
    max_concurrency: int = 4
    temperature: float | None = None
    supports_parallel_tool_calls: bool = False
    visible_to_users: bool = True
    scenario_label: str | None = None
    scenario_description: str | None = None
    supports_vision: bool = False


@dataclass(frozen=True, slots=True)
class ModelProfileUpdateInput:
    """管理员提交的模型配置局部更新补丁；api_key 为 UNSET 或 None 时严格保留原密文。"""

    expected_version: int | None = None
    name: str | UnsetType = UNSET
    base_url: str | UnsetType = UNSET
    model: str | UnsetType = UNSET
    api_key: str | None | UnsetType = field(default=UNSET, repr=False)
    context_window_tokens: int | UnsetType = UNSET
    tokenizer_encoding: Literal["cl100k_base", "o200k_base"] | UnsetType = UNSET
    supports_stream: bool | UnsetType = UNSET
    supports_tools: bool | UnsetType = UNSET
    supports_parallel_tool_calls: bool | UnsetType = UNSET
    max_output_tokens: int | UnsetType = UNSET
    request_timeout_seconds: int | UnsetType = UNSET
    execution_timeout_seconds: int | UnsetType = UNSET
    max_concurrency: int | UnsetType = UNSET
    temperature: float | None | UnsetType = UNSET
    enabled: bool | UnsetType = UNSET
    visible_to_users: bool | UnsetType = UNSET
    scenario_label: str | None | UnsetType = UNSET
    scenario_description: str | None | UnsetType = UNSET
    supports_vision: bool | UnsetType = UNSET

    def has_updates(self) -> bool:
        """判断是否至少包含一个非 expected_version 的显式更新字段。"""
        return any(
            value is not UNSET
            for value in (
                self.name,
                self.base_url,
                self.model,
                self.api_key,
                self.context_window_tokens,
                self.tokenizer_encoding,
                self.supports_stream,
                self.supports_tools,
                self.supports_parallel_tool_calls,
                self.max_output_tokens,
                self.request_timeout_seconds,
                self.execution_timeout_seconds,
                self.max_concurrency,
                self.temperature,
                self.enabled,
                self.visible_to_users,
                self.scenario_label,
                self.scenario_description,
                self.supports_vision,
            )
        )


@dataclass(frozen=True, slots=True)
class ValidatedModelProfileFields:
    """经过完整领域不变量复核并规范化后的模型配置字段集合。"""

    name: str
    base_url: str
    model: str
    context_window_tokens: int
    tokenizer_encoding: Literal["cl100k_base", "o200k_base"]
    supports_stream: bool
    supports_tools: bool
    supports_parallel_tool_calls: bool
    max_output_tokens: int
    request_timeout_seconds: int
    execution_timeout_seconds: int
    max_concurrency: int
    temperature: float | None
    enabled: bool
    visible_to_users: bool
    scenario_label: str | None
    scenario_description: str | None
    supports_vision: bool


def normalize_optional_scenario_text(value: str | None, *, maximum: int) -> str | None:
    """校验场景标签与说明：拒绝全部 Cc 控制字符、超长，空白字符串归一为 None。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ModelProfileError()
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ModelProfileError()
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > maximum:
        raise ModelProfileError()
    return normalized


def validate_model_profile_fields(
    *,
    name: str,
    base_url: str,
    model: str,
    context_window_tokens: int,
    tokenizer_encoding: str,
    supports_stream: bool,
    supports_tools: bool,
    supports_parallel_tool_calls: bool,
    max_output_tokens: int,
    request_timeout_seconds: int,
    execution_timeout_seconds: int,
    max_concurrency: int,
    temperature: float | None,
    enabled: bool,
    visible_to_users: bool,
    scenario_label: str | None,
    scenario_description: str | None,
    supports_vision: bool,
    require_output_strictly_less_than_context: bool = False,
) -> ValidatedModelProfileFields:
    """复核模型配置全部领域不变量，用于创建与局部编辑合并后的统一校验。"""
    try:
        parsed = urlsplit(base_url)
        valid_url = (
            isinstance(base_url, str)
            and len(base_url) <= 2048
            and parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except (TypeError, ValueError):
        valid_url = False

    valid_temp = temperature is None or (
        not isinstance(temperature, bool)
        and isinstance(temperature, (int, float))
        and math.isfinite(temperature)
        and 0 <= float(temperature) <= 2
    )
    output_upper = (
        min(context_window_tokens - 1, MAX_OUTPUT_TOKENS)
        if require_output_strictly_less_than_context
        else min(context_window_tokens, MAX_OUTPUT_TOKENS)
    )
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > 120
        or any(unicodedata.category(ch) == "Cc" for ch in name)
        or not isinstance(model, str)
        or not model.strip()
        or len(model) > 200
        or any(unicodedata.category(ch) == "Cc" for ch in model)
        or not valid_url
        or type(context_window_tokens) is not int
        or not 1 <= context_window_tokens <= MAX_CONTEXT_WINDOW_TOKENS
        or tokenizer_encoding not in {"cl100k_base", "o200k_base"}
        or type(supports_stream) is not bool
        or type(supports_tools) is not bool
        or type(supports_parallel_tool_calls) is not bool
        or type(enabled) is not bool
        or type(visible_to_users) is not bool
        or type(supports_vision) is not bool
        or (supports_parallel_tool_calls and not supports_tools)
        or type(max_output_tokens) is not int
        or not 1 <= max_output_tokens <= output_upper
        or type(request_timeout_seconds) is not int
        or not 1 <= request_timeout_seconds <= 3600
        or type(execution_timeout_seconds) is not int
        or not 1 <= execution_timeout_seconds <= 3600
        or type(max_concurrency) is not int
        or not 1 <= max_concurrency <= 128
        or not valid_temp
    ):
        raise ModelProfileError()

    normalized_label = normalize_optional_scenario_text(
        scenario_label,
        maximum=MAX_SCENARIO_LABEL_LENGTH,
    )
    normalized_description = normalize_optional_scenario_text(
        scenario_description,
        maximum=MAX_SCENARIO_DESCRIPTION_LENGTH,
    )
    encoding: Literal["cl100k_base", "o200k_base"] = (
        "cl100k_base" if tokenizer_encoding == "cl100k_base" else "o200k_base"
    )
    return ValidatedModelProfileFields(
        name=name,
        base_url=base_url,
        model=model,
        context_window_tokens=context_window_tokens,
        tokenizer_encoding=encoding,
        supports_stream=supports_stream,
        supports_tools=supports_tools,
        supports_parallel_tool_calls=supports_parallel_tool_calls,
        max_output_tokens=max_output_tokens,
        request_timeout_seconds=request_timeout_seconds,
        execution_timeout_seconds=execution_timeout_seconds,
        max_concurrency=max_concurrency,
        temperature=None if temperature is None else float(temperature),
        enabled=enabled,
        visible_to_users=visible_to_users,
        scenario_label=normalized_label,
        scenario_description=normalized_description,
        supports_vision=supports_vision,
    )


def validate_model_profile_api_key(value: str) -> None:
    """验证 API Key 非空白、无 NUL 且可放入加密信封。"""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ModelProfileError("llm_model_credential_invalid")
    try:
        serialized = json.dumps(
            {"api_key": value},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except UnicodeError:
        raise ModelProfileError("llm_model_credential_invalid") from None
    if len(serialized) > MAX_MCP_SECRET_BYTES:
        raise ModelProfileError("llm_model_credential_invalid")


__all__ = [
    "MAX_CONTEXT_WINDOW_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "MAX_SCENARIO_DESCRIPTION_LENGTH",
    "MAX_SCENARIO_LABEL_LENGTH",
    "ModelProfileInput",
    "ModelProfileUpdateInput",
    "ModelProfileVersion",
    "RuntimeModelProfile",
    "ValidatedModelProfileFields",
    "normalize_optional_scenario_text",
    "validate_model_profile_api_key",
    "validate_model_profile_fields",
]
