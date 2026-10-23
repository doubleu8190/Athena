"""大语言模型调用端口。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

StructuredOutputSchema = type[Any] | Mapping[str, Any]


class LLMPort(Protocol):
    async def complete(self, messages: list[Any], **kwargs: Any) -> Any: ...
    async def invoke(self, messages: list[Any], **kwargs: Any) -> Any: ...
    async def invoke_structured(
        self,
        messages: list[Any],
        schema: StructuredOutputSchema,
        **kwargs: Any,
    ) -> Any: ...
    def count_text_tokens(self, text: str) -> int: ...


__all__ = ["LLMPort", "StructuredOutputSchema"]
