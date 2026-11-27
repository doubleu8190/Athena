"""局部更新场景统一使用的未赋值哨兵原语。

用于严格区分“调用方未传入该字段（保持原值）”与“显式传入 None（清空字段）”。
采用单成员 Enum 实现语言级真单例，同时满足：
1. `value is UNSET` / `value is not UNSET` 在 mypy --strict 下的精确类型收窄；
2. `isinstance(value, UnsetType)` 在运行期与类型检查期的兼容使用。
"""

from __future__ import annotations

from enum import Enum
from typing import Final, Literal


class UnsetType(Enum):
    """全局唯一的“未传入”哨兵枚举类型。"""

    TOKEN = "UNSET"

    def __repr__(self) -> str:
        return "UNSET"


# 全系统共享的唯一未赋值哨兵实例，禁止在各业务模块中重复定义局部哨兵。
UNSET: Final[Literal[UnsetType.TOKEN]] = UnsetType.TOKEN

__all__ = ["UNSET", "UnsetType"]
