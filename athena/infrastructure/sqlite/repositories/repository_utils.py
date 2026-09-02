"""SQLite 仓库共享的时间和 JSON 工具。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)

# 区分未传参和显式传 None。
_SENTINEL = object()


def _now_iso() -> str:
    """返回当前时间的 ISO 格式字符串。"""
    return datetime.now().isoformat()


def _json_dumps(value: Any) -> str:
    """把值转换成 JSON 文本；Pydantic 模型会先转换成普通 JSON 数据。"""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_loads(value: str | None, default: Any) -> Any:
    """读取 JSON 文本；内容为空或格式不正确时返回默认值。"""
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _json_loads_model(
    value: str | None, model: type[ModelT], default: ModelT
) -> ModelT:
    """把 JSON 文本读取为指定模型；旧数据损坏时使用默认模型。"""
    if not value:
        return default
    try:
        return model.model_validate_json(value)
    except (ValueError, ValidationError):
        return default
