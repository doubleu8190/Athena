"""管理员模型配置接口 DTO。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from domain.llm.model.compression_policy import (
    DEFAULT_RECENT_RATIO,
    DEFAULT_RECENT_TOKEN_CAP,
    DEFAULT_SUMMARY_RATIO,
    DEFAULT_SUMMARY_TOKEN_CAP,
    DEFAULT_TRIGGER_TOKENS,
    MAX_COMPRESSION_TOKEN_BUDGET,
)
from domain.llm.model.profile import (
    MAX_CONTEXT_WINDOW_TOKENS,
    MAX_OUTPUT_TOKENS,
    MAX_SCENARIO_DESCRIPTION_LENGTH,
    MAX_SCENARIO_LABEL_LENGTH,
)


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
    visible_to_users: bool = True
    scenario_label: str | None = Field(default=None, max_length=MAX_SCENARIO_LABEL_LENGTH)
    scenario_description: str | None = Field(
        default=None, max_length=MAX_SCENARIO_DESCRIPTION_LENGTH
    )
    supports_vision: bool = False


class ModelProfileUpdateRequest(BaseModel):
    """局部编辑模型配置请求；api_key 未传或为 null 时严格保留原加密凭据。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    expected_version: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = Field(default=None, min_length=8, max_length=2048)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: str | None = Field(default=None, min_length=1, max_length=16384, repr=False)
    context_window_tokens: int | None = Field(default=None, ge=1, le=MAX_CONTEXT_WINDOW_TOKENS)
    tokenizer_encoding: str | None = Field(default=None, pattern="^(cl100k_base|o200k_base)$")
    supports_stream: bool | None = None
    supports_tools: bool | None = None
    supports_parallel_tool_calls: bool | None = None
    max_output_tokens: int | None = Field(default=None, ge=1, le=MAX_OUTPUT_TOKENS)
    request_timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    execution_timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    max_concurrency: int | None = Field(default=None, ge=1, le=128)
    temperature: float | None = Field(default=None, ge=0, le=2)
    enabled: bool | None = None
    visible_to_users: bool | None = None
    scenario_label: str | None = Field(default=None, max_length=MAX_SCENARIO_LABEL_LENGTH)
    scenario_description: str | None = Field(
        default=None, max_length=MAX_SCENARIO_DESCRIPTION_LENGTH
    )
    supports_vision: bool | None = None


class ModelProfileResponse(BaseModel):
    """不包含凭据明文的模型版本信息。"""

    id: str
    name: str
    protocol: str
    base_url: str
    model: str
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
    visible_to_users: bool
    scenario_label: str | None
    scenario_description: str | None
    supports_vision: bool
    version: int
    created_at: datetime
    updated_at: datetime


class UserModelCatalogItemResponse(BaseModel):
    """用户侧只读场景化模型目录项，严格隐藏供应商地址、密钥状态及运维参数。"""

    id: str
    name: str
    scenario_label: str | None
    scenario_description: str | None
    is_default: bool
    supports_vision: bool


class UserModelCatalogResponse(BaseModel):
    """用户侧可用模型目录及系统默认模型标识。"""

    items: list[UserModelCatalogItemResponse]
    default_model_id: str | None


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
    trigger_tokens: int = Field(
        default=DEFAULT_TRIGGER_TOKENS,
        ge=1,
        le=MAX_COMPRESSION_TOKEN_BUDGET,
    )
    recent_ratio: float = Field(default=DEFAULT_RECENT_RATIO, gt=0, lt=1)
    recent_token_cap: int = Field(
        default=DEFAULT_RECENT_TOKEN_CAP,
        ge=1,
        le=MAX_COMPRESSION_TOKEN_BUDGET,
    )
    summary_ratio: float = Field(default=DEFAULT_SUMMARY_RATIO, gt=0, lt=1)
    summary_token_cap: int = Field(
        default=DEFAULT_SUMMARY_TOKEN_CAP,
        ge=1,
        le=MAX_COMPRESSION_TOKEN_BUDGET,
    )


class CompressionPolicyResponse(BaseModel):
    """不含凭据的当前压缩策略安全投影。"""

    id: str | None
    compression_model_profile_id: str | None
    trigger_tokens: int | None
    recent_ratio: float | None
    recent_token_cap: int | None
    summary_ratio: float | None
    summary_token_cap: int | None
    created_at: datetime | None


__all__ = [
    "ModelProfileCreateRequest",
    "ModelProfileUpdateRequest",
    "CompressionPolicyRequest",
    "CompressionPolicyResponse",
    "ModelProfileDefaultRequest",
    "ModelProfileEnabledRequest",
    "ModelProfileListResponse",
    "ModelProfileResponse",
    "UserModelCatalogItemResponse",
    "UserModelCatalogResponse",
]
