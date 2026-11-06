"""部署管理的 LLM Profile 配置。"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any

from domain.llm.model import LlmConfigurationError

DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
DEFAULT_STREAM_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_MAX_INPUT_BYTES = 256 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_MAX_OUTPUT_TOKENS = 8192
DEFAULT_MAX_TOOLS = 32
DEFAULT_MAX_TOOL_ARGUMENT_BYTES = 64 * 1024
PROFILE_KEYS = frozenset(
    {
        "base_url",
        "api_key_env",
        "model",
        "capabilities",
        "request_timeout_seconds",
        "stream_timeout_seconds",
        "max_concurrency",
        "max_input_bytes",
        "max_response_bytes",
        "max_output_tokens",
        "max_tools",
        "max_tool_argument_bytes",
    }
)
CAPABILITY_KEYS = frozenset({"tools", "stream", "structured", "temperature"})
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROFILE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class LlmProfile:
    """解析并校验后的部署 Profile。"""

    name: str
    api_key: str = field(repr=False)
    model: str
    base_url: str | None
    capabilities: frozenset[str]
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    stream_timeout_seconds: float = DEFAULT_STREAM_TIMEOUT_SECONDS
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    max_tools: int = DEFAULT_MAX_TOOLS
    max_tool_argument_bytes: int = DEFAULT_MAX_TOOL_ARGUMENT_BYTES


def parse_profiles(
    raw: str | None = None, environ: dict[str, str] | None = None
) -> dict[str, LlmProfile]:
    """解析唯一配置变量，严格拒绝未知字段、重复键和缺失凭据。"""
    value = os.getenv("ATHENA_LLM_PROFILES") if raw is None else raw
    if not value or not value.strip():
        return {}
    variables = os.environ if environ is None else environ

    def pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = item
        return result

    try:
        data = json.loads(value, object_pairs_hook=pairs_no_duplicates)
        if not isinstance(data, dict) or not data:
            raise ValueError
        profiles: dict[str, LlmProfile] = {}
        for name, item in data.items():
            if (
                not isinstance(name, str)
                or not PROFILE_NAME.fullmatch(name)
                or not isinstance(item, dict)
            ):
                raise ValueError
            if set(item) - PROFILE_KEYS:
                raise ValueError
            key_env = item.get("api_key_env")
            model = item.get("model")
            caps = item.get("capabilities")
            if not isinstance(key_env, str) or not ENV_NAME.fullmatch(key_env):
                raise ValueError
            key = variables.get(key_env)
            if not key:
                raise ValueError
            if not isinstance(model, str) or not model.strip() or len(model) > 200:
                raise ValueError
            if not isinstance(caps, dict) or set(caps) - CAPABILITY_KEYS:
                raise ValueError
            if any(type(flag) is not bool for flag in caps.values()):
                raise ValueError
            base_url = item.get("base_url")
            if base_url is not None and (
                not isinstance(base_url, str) or not base_url.startswith("https://")
            ):
                raise ValueError
            timeout_values: dict[str, float] = {}
            for field_name, default in (
                ("request_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS),
                ("stream_timeout_seconds", DEFAULT_STREAM_TIMEOUT_SECONDS),
            ):
                number = item.get(field_name, default)
                if type(number) not in (int, float) or not math.isfinite(number) or number <= 0:
                    raise ValueError
                if number > 3600:
                    raise ValueError
                timeout_values[field_name] = float(number)
            integer_defaults = {
                "max_concurrency": DEFAULT_MAX_CONCURRENCY,
                "max_input_bytes": DEFAULT_MAX_INPUT_BYTES,
                "max_response_bytes": DEFAULT_MAX_RESPONSE_BYTES,
                "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
                "max_tools": DEFAULT_MAX_TOOLS,
                "max_tool_argument_bytes": DEFAULT_MAX_TOOL_ARGUMENT_BYTES,
            }
            integer_values: dict[str, int] = {}
            for field_name, default in integer_defaults.items():
                number = item.get(field_name, default)
                if type(number) is not int or number < 1:
                    raise ValueError
                integer_values[field_name] = number
            if integer_values["max_concurrency"] > 128 or integer_values["max_tools"] > 128:
                raise ValueError
            if (
                integer_values["max_input_bytes"] > 16 * 1024 * 1024
                or integer_values["max_response_bytes"] > 16 * 1024 * 1024
                or integer_values["max_tool_argument_bytes"] > 16 * 1024 * 1024
                or integer_values["max_output_tokens"] > 1_000_000
            ):
                raise ValueError
            profiles[name] = LlmProfile(
                name=name,
                api_key=key,
                model=model.strip(),
                base_url=base_url,
                capabilities=frozenset(k for k, enabled in caps.items() if enabled),
                request_timeout_seconds=timeout_values["request_timeout_seconds"],
                stream_timeout_seconds=timeout_values["stream_timeout_seconds"],
                max_concurrency=integer_values["max_concurrency"],
                max_input_bytes=integer_values["max_input_bytes"],
                max_response_bytes=integer_values["max_response_bytes"],
                max_output_tokens=integer_values["max_output_tokens"],
                max_tools=integer_values["max_tools"],
                max_tool_argument_bytes=integer_values["max_tool_argument_bytes"],
            )
        return profiles
    except (TypeError, ValueError, json.JSONDecodeError):
        raise LlmConfigurationError() from None
