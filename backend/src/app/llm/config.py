"""LLM 客户端使用的不可变配置对象。"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
DEFAULT_STREAM_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_MAX_INPUT_BYTES = 256 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_MAX_OUTPUT_TOKENS = 8192
DEFAULT_MAX_TOOLS = 32
DEFAULT_MAX_TOOL_ARGUMENT_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class LlmProfile:
    """由调用方显式传入 LLM 服务的配置，不从部署环境解析。"""

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
