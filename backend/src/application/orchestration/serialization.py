"""编排用例之间共享的 JSON 值转换。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel


def json_value(value: Any) -> Any:
    """将领域对象递归转换为可 JSON 编码的基本值。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_value(item) for item in value]
    if isinstance(value, BaseModel):
        return json_value(value.model_dump())
    if isinstance(value, Enum):
        return json_value(value.value)
    return str(value)


def as_dict(value: Any) -> dict[str, Any]:
    """将对象转换为字典；非对象值统一放入 ``value`` 字段。"""
    converted = json_value(value)
    if isinstance(converted, Mapping):
        return dict(converted)
    if converted is None:
        return {}
    return {"value": converted}
__all__ = ["as_dict", "json_value"]
