"""上下文压缩策略的类型化输入与版本投影。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CompressionPolicyInput:
    """管理员提交的压缩策略字段。"""

    compression_model_profile_id: str
    trigger_ratio: float = 0.8
    recent_ratio: float = 0.2
    summary_ratio: float = 0.1


@dataclass(frozen=True, slots=True)
class CompressionPolicyVersion:
    """不可变压缩策略版本。"""

    id: str
    compression_model_profile_id: str
    trigger_ratio: float
    recent_ratio: float
    summary_ratio: float
    prompt_template_version: str
    created_at: datetime


__all__ = ["CompressionPolicyInput", "CompressionPolicyVersion"]
