from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .contracts import ContextBundle, ContextItem, ContextPlan, ProviderResult


class ContextAcquisitionService:
    """Run selected context providers concurrently under one budget."""

    def __init__(self, providers: dict[str, Callable[..., Awaitable[ProviderResult]]], token_counter=None) -> None:
        self._providers = dict(providers)
        self._token_counter = token_counter or (lambda value: len(value.split()))

    async def acquire(self, *, session_id: str, task: Any, plan: ContextPlan, run_id: str | None = None) -> ContextBundle:
        selected = [(name, self._providers[name]) for name in plan.providers if name in self._providers]
        unknown = [name for name in plan.providers if name not in self._providers]
        values = await asyncio.gather(
            *(provider(session_id=session_id, task=task, plan=plan, run_id=run_id) for _, provider in selected),
            return_exceptions=True,
        )
        results = [ProviderResult(name, "skipped", error_message="provider unavailable") for name in unknown]
        for (name, _), value in zip(selected, values):
            results.append(ProviderResult(name, "failed", error_message=str(value)) if isinstance(value, BaseException) else value)
        items = [item for result in results for item in result.items]
        truncated = len(items) > plan.max_items
        items = items[:plan.max_items]
        kept: list[ContextItem] = []
        tokens = 0
        for item in items:
            count = self._token_counter(item.content)
            if tokens + count > plan.max_tokens:
                truncated = True
                break
            kept.append(item)
            tokens += count
        return ContextBundle(tuple(kept), tuple(results), tuple(item.provider for item in results if item.status == "succeeded"), tuple(item.provider for item in results if item.status in {"failed", "timeout"}), truncated)


__all__ = ["ContextAcquisitionService"]
