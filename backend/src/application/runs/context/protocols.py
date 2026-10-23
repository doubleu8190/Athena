"""上下文采集能力的应用协议。"""

from __future__ import annotations

from typing import Protocol

from .contracts import ContextPlan, ProviderResult


class ContextProvider(Protocol):
    name: str

    async def retrieve_context(
        self,
        *,
        session_id: str,
        run_id: str,
        plan: ContextPlan,
    ) -> ProviderResult: ...


__all__ = ["ContextProvider"]
