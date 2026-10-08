from __future__ import annotations

import asyncio
from typing import Any

from .providers import ContextProvider

from .contracts import ContextBundle, ContextItem, ContextPlan, ProviderResult


class ContextAcquisitionService:
    """Run selected context providers concurrently under one budget."""

    def __init__(
        self,
        providers: dict[str, ContextProvider],
        token_counter=None,
        trace_repository: Any = None,
    ) -> None:
        self._providers = dict(providers)
        self._token_counter = token_counter or (lambda value: len(value.split()))
        self._trace_repository = trace_repository

    async def retrieve_context(
        self,
        *,
        session_id: str,
        task: Any,
        plan: ContextPlan,
        run_id: str | None = None,
    ) -> ContextBundle:
        selected = [
            (name, self._providers[name])
            for name in plan.providers
            if name in self._providers
        ]
        unknown = [name for name in plan.providers if name not in self._providers]
        values = await asyncio.gather(
            *(
                (
                    provider(
                        session_id=session_id,
                        task=task,
                        plan=plan,
                        run_id=run_id,
                    )
                    if callable(provider)
                    else provider.acquire(
                        session_id=session_id,
                        task=task,
                        plan=plan,
                        run_id=run_id,
                    )
                )
                for _, provider in selected
            ),
            return_exceptions=True,
        )
        results = [
            ProviderResult(name, "skipped", error_message="provider unavailable")
            for name in unknown
        ]
        for (name, _), value in zip(selected, values):
            results.append(
                ProviderResult(name, "failed", error_message=str(value))
                if isinstance(value, BaseException)
                else value
            )
        items = [item for result in results for item in result.items]
        truncated = len(items) > plan.max_items
        items = items[: plan.max_items]
        kept: list[ContextItem] = []
        tokens = 0
        for item in items:
            count = self._token_counter(item.content)
            if tokens + count > plan.max_tokens:
                truncated = True
                break
            kept.append(item)
            tokens += count
        bundle = ContextBundle(
            tuple(kept),
            tuple(results),
            tuple(item.provider for item in results if item.status == "succeeded"),
            tuple(
                item.provider
                for item in results
                if item.status in {"failed", "timeout"}
            ),
            truncated,
        )
        if self._trace_repository is not None and hasattr(
            self._trace_repository, "record_context"
        ):
            try:
                await self._trace_repository.record_context(
                    session_id=session_id,
                    agent_run_id=run_id,
                    query=str(
                        getattr(task, "goal", "")
                        if not isinstance(task, dict)
                        else task.get("goal", "")
                    ),
                    provider_results=results,
                    items=kept,
                )
            except Exception:
                pass
        return bundle

    async def acquire(
        self,
        *,
        session_id: str,
        task: Any,
        plan: ContextPlan,
        run_id: str | None = None,
    ) -> ContextBundle:
        """Compatibility alias for callers migrated from the original API."""
        return await self.retrieve_context(
            session_id=session_id, task=task, plan=plan, run_id=run_id
        )


__all__ = ["ContextAcquisitionService"]
