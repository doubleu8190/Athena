"""Run 使用的不可变模型配置版本。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class ModelProfileVersion:
    """不含凭据明文的模型版本投影。"""

    id: str
    name: str
    protocol: Literal["chat_completions"]
    base_url: str
    model: str
    credential_env: str
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
    credential_env: str
    context_window_tokens: int
    max_output_tokens: int
    tokenizer_encoding: Literal["cl100k_base", "o200k_base"] = "o200k_base"
    supports_stream: bool = True
    supports_tools: bool = True
    request_timeout_seconds: int = 120
    execution_timeout_seconds: int = 300
    max_concurrency: int = 4
    temperature: float | None = None


__all__ = ["ModelProfileInput", "ModelProfileVersion", "RuntimeModelProfile"]
