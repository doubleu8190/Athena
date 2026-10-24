"""大语言模型调用端口。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol

from .llm_stream import LLMStreamEvent

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
    def stream(
        self, messages: list[Any], **kwargs: Any
    ) -> AsyncIterator[LLMStreamEvent]: ...
    def count_text_tokens(self, text: str) -> int: ...


__all__ = ["LLMPort", "StructuredOutputSchema"]
