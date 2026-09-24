"""PostgreSQL 仓库共享的时间和 JSON 工具。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)

# 区分未传参和显式传 None。
_SENTINEL = object()


def _strip_nul(value: str) -> str:
    """移除 PostgreSQL ``TEXT`` 不允许保存的 NUL 字符。

    参数：
        value (str): 即将写入 PostgreSQL 文本列的字符串。

    返回值：
        str: 删除所有 ``U+0000`` 后的文本；其他字符保持不变。

    异常：
        不主动抛出业务异常。
    """
    return value.replace("\x00", "")


def _now_iso() -> str:
    """返回当前时间的 ISO 格式字符串。"""
    return datetime.now().isoformat()


def _json_dumps(value: Any) -> str:
    """把值递归转换为 JSON 原生值后序列化。

    Pydantic 模型经常嵌套在列表或字典中（例如 ``list[ToolCall]``）。
    只处理顶层模型会触发 ``json.dumps(default=str)``，把嵌套模型保存成
    ``"id='...' ..."`` 这样的不可恢复字符串。
    """

    def to_json_value(item: Any) -> Any:
        if isinstance(item, BaseModel):
            return to_json_value(item.model_dump(mode="json", exclude_none=True))
        if isinstance(item, str):
            return _strip_nul(item)
        if isinstance(item, dict):
            return {str(key): to_json_value(val) for key, val in item.items()}
        if isinstance(item, (list, tuple, set, frozenset)):
            return [to_json_value(val) for val in item]
        return item

    value = to_json_value(value)
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
