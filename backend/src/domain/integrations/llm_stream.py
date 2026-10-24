"""不依赖供应商 SDK 的模型流事件。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias


@dataclass(frozen=True, slots=True)
class LLMTextDelta:
    """模型返回的一段用户可见文本。"""

    text: str


@dataclass(frozen=True, slots=True)
class LLMStreamEnd:
    """模型流结束原因；只有 stop 表示正常完成。"""

    finish_reason: Literal["stop", "length", "filtered", "unknown"]


LLMStreamEvent: TypeAlias = LLMTextDelta | LLMStreamEnd


__all__ = ["LLMTextDelta", "LLMStreamEnd", "LLMStreamEvent"]
