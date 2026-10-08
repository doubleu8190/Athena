"""大语言模型调用端口。"""

from __future__ import annotations

from typing import Any, Protocol


class LLMPort(Protocol):
    async def complete(self, messages: list[Any], **kwargs: Any) -> Any: ...
    async def invoke(self, messages: list[Any], **kwargs: Any) -> Any: ...
    def count_text_tokens(self, text: str) -> int: ...


__all__ = ["LLMPort"]
