"""测试用大语言模型替身。"""

from __future__ import annotations

from typing import Any


class _EchoModel:
    """返回最后一条消息内容的异步模型。"""

    async def invoke(self, messages: list[Any], **_kwargs: Any) -> dict[str, str]:
        """返回最后一条消息内容；无消息时返回空内容。"""
        value = messages[-1] if messages else ""
        if isinstance(value, dict):
            value = value.get("content", "")
        return {"content": str(value)}


__all__ = ["_EchoModel"]
