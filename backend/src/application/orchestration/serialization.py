"""编排领域对象转换为 checkpoint 字典的辅助方法。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shared.serialization import json_value


def as_dict(value: Any) -> dict[str, Any]:
    """将对象转换为字典；非对象值统一放入 ``value`` 字段。"""
    converted = json_value(value)
    if isinstance(converted, Mapping):
        return dict(converted)
    if converted is None:
        return {}
    return {"value": converted}


__all__ = ["as_dict"]
