"""Default Root Agent bridge for configured language models."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ConfiguredRootAgent:
    """Translate Root harness input into one configured LLM invocation."""

    def __init__(self, llm: Any) -> None:
        """保存 LLM provider；不在构造阶段建立外部连接。"""
        self._llm = llm

    async def __call__(self, *, harness_input: Mapping[str, Any], **_kwargs: Any) -> dict[str, Any]:
        """执行一轮 Root LLM 调用并返回 JSON-safe 响应。"""
        messages = list(harness_input.get("messages", []))
        context = harness_input.get("context", {})
        if isinstance(context, Mapping) and context.get("items"):
            messages.insert(
                0,
                {
                    "role": "system",
                    "content": "Use the supplied context as reference material.",
                },
            )
        value = await self._llm.invoke(messages)
        if isinstance(value, Mapping):
            content = value.get("content", value.get("result", value))
        else:
            content = getattr(value, "content", value)
        return {"response": {"content": str(content)}}


__all__ = ["ConfiguredRootAgent"]
