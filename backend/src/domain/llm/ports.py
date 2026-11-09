"""LLM 供应商适配器端口。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from domain.llm.model.profile import RuntimeModelProfile
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


class LlmStreamProvider(Protocol):
    """Run 所需的单次流式调用能力。"""

    def stream(
        self, runtime: RuntimeModelProfile, request: LlmRequest
    ) -> AbstractAsyncContextManager[AsyncIterator[LlmStreamEvent]]:
        """持有上游流和并发配额直至消费或关闭。"""


class LlmRuntimeTextProvider(Protocol):
    """固定模型版本的无工具文本请求能力。"""

    async def complete(self, runtime: RuntimeModelProfile, request: LlmRequest) -> LlmResponse:
        """执行一次完整的无工具文本请求。"""
