"""Run 使用的不可变模型配置版本。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# 上下文窗口与单次输出有各自的上限，扩大上下文容量不改变输出保护边界。
MAX_CONTEXT_WINDOW_TOKENS = 1_048_576
MAX_OUTPUT_TOKENS = 1_000_000


@dataclass(frozen=True, slots=True)
class ModelProfileVersion:
    """不含凭据明文的模型版本投影。"""

    id: str
    name: str
    protocol: Literal["chat_completions"]
    base_url: str
    model: str
    has_api_key: bool
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
    supports_parallel_tool_calls: bool = False


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


__all__ = [
    "MAX_CONTEXT_WINDOW_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "ModelProfileInput",
    "ModelProfileVersion",
    "RuntimeModelProfile",
]
