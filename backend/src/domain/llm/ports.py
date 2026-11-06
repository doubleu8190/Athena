"""LLM 供应商适配器端口。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from domain.llm.model.types import LlmRequest, LlmResponse, LlmStreamEvent


class LlmProvider(Protocol):
    """单次调用供应商端口。"""

    async def complete(self, profile: str, request: LlmRequest) -> LlmResponse: ...

    def stream(
        self, profile: str, request: LlmRequest
    ) -> AbstractAsyncContextManager[AsyncIterator[LlmStreamEvent]]: ...

    async def generate_structured(
        self, profile: str, request: LlmRequest, schema_name: str, schema: Mapping[str, Any]
    ) -> LlmResponse: ...
