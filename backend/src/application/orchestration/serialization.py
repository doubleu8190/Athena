"""编排领域对象转换为 checkpoint 字典的辅助方法。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shared.serialization import json_value


def as_dict(value: Any) -> dict[str, Any]:
    """将对象转换为字典；非对象值统一放入 ``value`` 字段。

    参数：
    - `value`（Any）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - dict[str, Any]：包含对象公开字段且适合 JSON 序列化的字典。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    converted = json_value(value)
    if isinstance(converted, Mapping):
        return dict(converted)
    if converted is None:
        return {}
    return {"value": converted}


__all__ = ["as_dict"]
