"""管理员模型配置接口 DTO。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from domain.llm.model.profile import MAX_CONTEXT_WINDOW_TOKENS, MAX_OUTPUT_TOKENS


class ModelProfileCreateRequest(BaseModel):
    """创建 Chat Completions 配置版本。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=8, max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    api_key: str = Field(min_length=1, max_length=16384, repr=False)
    context_window_tokens: int = Field(ge=1, le=MAX_CONTEXT_WINDOW_TOKENS)
    tokenizer_encoding: str = Field(default="o200k_base", pattern="^(cl100k_base|o200k_base)$")
    supports_stream: bool = True
    supports_tools: bool = True
    supports_parallel_tool_calls: bool = False
    max_output_tokens: int = Field(ge=1, le=MAX_OUTPUT_TOKENS)
    request_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    execution_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    max_concurrency: int = Field(default=4, ge=1, le=128)
    temperature: float | None = Field(default=None, ge=0, le=2)


class ModelProfileResponse(BaseModel):
    """不包含凭据明文的模型版本信息。"""

    id: str
    name: str
    protocol: str
    base_url: str
    model: str
    has_api_key: bool
    context_window_tokens: int
    tokenizer_encoding: str
    supports_stream: bool
    supports_tools: bool
    supports_parallel_tool_calls: bool
    max_output_tokens: int
    request_timeout_seconds: int
    execution_timeout_seconds: int
    max_concurrency: int
    temperature: float | None
    enabled: bool
    created_at: datetime


class ModelProfileListResponse(BaseModel):
    """模型配置列表及默认版本标识。"""

    items: list[ModelProfileResponse]
    default_profile_id: str | None


class ModelProfileEnabledRequest(BaseModel):
    """启用或停用一个版本。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool


class ModelProfileDefaultRequest(BaseModel):
    """设置默认版本。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    profile_id: str = Field(min_length=1, max_length=64)


class CompressionPolicyRequest(BaseModel):
    """设置管理员管理的上下文压缩策略。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    compression_model_profile_id: str = Field(min_length=1, max_length=64)
    trigger_ratio: float = Field(default=0.8, gt=0, lt=1)
    recent_ratio: float = Field(default=0.2, gt=0, lt=1)
    summary_ratio: float = Field(default=0.1, gt=0, lt=1)


class CompressionPolicyResponse(BaseModel):
    """不含凭据的当前压缩策略安全投影。"""

    id: str | None
    compression_model_profile_id: str | None
    trigger_ratio: float | None
    recent_ratio: float | None
    summary_ratio: float | None
    prompt_template_version: str | None
    created_at: datetime | None


__all__ = [
    "ModelProfileCreateRequest",
    "CompressionPolicyRequest",
    "CompressionPolicyResponse",
    "ModelProfileDefaultRequest",
    "ModelProfileEnabledRequest",
    "ModelProfileListResponse",
    "ModelProfileResponse",
]
