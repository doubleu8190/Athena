"""上下文压缩策略的类型化输入与版本投影。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

DEFAULT_TRIGGER_TOKENS = 262_144
DEFAULT_RECENT_RATIO = 0.2
DEFAULT_RECENT_TOKEN_CAP = 32_768
DEFAULT_SUMMARY_RATIO = 0.1
DEFAULT_SUMMARY_TOKEN_CAP = 8_192
# 三个 Token 预算也由浏览器提交；统一限制在 JavaScript 安全整数范围内，
# 避免前端精度丢失或 PostgreSQL BigInteger 写入越界后变成 500。
MAX_COMPRESSION_TOKEN_BUDGET = (1 << 53) - 1


@dataclass(frozen=True, slots=True)
class CompressionPolicyInput:
    """管理员提交的压缩策略字段。"""

    compression_model_profile_id: str
    trigger_tokens: int = DEFAULT_TRIGGER_TOKENS
    recent_ratio: float = DEFAULT_RECENT_RATIO
    recent_token_cap: int = DEFAULT_RECENT_TOKEN_CAP
    summary_ratio: float = DEFAULT_SUMMARY_RATIO
    summary_token_cap: int = DEFAULT_SUMMARY_TOKEN_CAP


@dataclass(frozen=True, slots=True)
class CompressionPolicyVersion:
    """不可变压缩策略版本。"""

    id: str
    compression_model_profile_id: str
    trigger_tokens: int
    recent_ratio: float
    recent_token_cap: int
    summary_ratio: float
    summary_token_cap: int
    prompt_template_version: str
    created_at: datetime


__all__ = [
    "CompressionPolicyInput",
    "CompressionPolicyVersion",
    "DEFAULT_RECENT_RATIO",
    "DEFAULT_RECENT_TOKEN_CAP",
    "DEFAULT_SUMMARY_RATIO",
    "DEFAULT_SUMMARY_TOKEN_CAP",
    "DEFAULT_TRIGGER_TOKENS",
    "MAX_COMPRESSION_TOKEN_BUDGET",
]
