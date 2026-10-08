"""上下文采集能力的应用协议。"""

from __future__ import annotations

from typing import Any, Protocol

from .contracts import ContextPlan, ProviderResult


class ContextProvider(Protocol):
    name: str

    async def retrieve_context(
        self,
        *,
        session_id: str,
        task: Any,
        plan: ContextPlan,
        run_id: str | None = None,
    ) -> ProviderResult: ...


__all__ = ["ContextProvider"]
