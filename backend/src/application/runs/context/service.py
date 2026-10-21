from __future__ import annotations

import asyncio
from time import perf_counter
from typing import Any

from .protocols import ContextProvider

from .contracts import ContextBundle, ContextItem, ContextPlan, ProviderResult


class ContextAcquisitionService:
    """Run selected context providers concurrently under one budget."""

    def __init__(
        self,
        providers: dict[str, ContextProvider],
        token_counter=None,
        trace_repository: Any = None,
        event_publisher: Any = None,
    ) -> None:
        self._providers = dict(providers)
        self._token_counter = token_counter or (lambda value: len(value.split()))
        self._trace_repository = trace_repository
        self._events = event_publisher

    async def retrieve_context(
        self,
        *,
        session_id: str,
        task: Any,
        plan: ContextPlan,
        run_id: str | None = None,
    ) -> ContextBundle:
        started_clock = perf_counter()
        await self._publish_event("retrieval.started", session_id, run_id, {
            "activity_id": f"retrieval:{run_id}",
            "activity_type": "retrieval",
            "name": "acquire_context",
            "parent_activity_id": f"run:{run_id or ''}",
            "status": "running",
        })
        selected = [
            (name, self._providers[name])
            for name in plan.providers
            if name in self._providers
        ]
        unknown = [name for name in plan.providers if name not in self._providers]
        values = await asyncio.gather(
            *(
                self._invoke_provider(name, provider, session_id=session_id, task=task, plan=plan, run_id=run_id)
                for name, provider in selected
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
        retrieval_run_id: str | None = None
        if self._trace_repository is not None and hasattr(
            self._trace_repository, "record_context"
        ):
            try:
                retrieval_run_id = await self._trace_repository.record_context(
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
        for result in results:
            provider_id = f"retrieval:{run_id}:provider:{result.provider}"
            await self._publish_event(
                "retrieval.provider.failed" if result.status in {"failed", "timeout"} else "retrieval.provider.completed",
                session_id,
                run_id,
                {
                    "activity_id": provider_id,
                    "activity_type": "retrieval_provider",
                    "name": result.provider,
                    "provider": result.provider,
                    "parent_activity_id": f"retrieval:{run_id or ''}",
                    "retrieval_run_id": retrieval_run_id,
                    "status": "failed" if result.status in {"failed", "timeout"} else result.status,
                    "candidate_count": result.candidate_count,
                    "result_count": result.result_count,
                    "duration_ms": int(result.duration_ms),
                },
            )
        has_success = any(result.status == "succeeded" for result in results)
        has_failure = any(result.status in {"failed", "timeout"} for result in results)
        await self._publish_event(
            "retrieval.completed" if has_success or not has_failure else "retrieval.failed",
            session_id,
            run_id,
            {
                "activity_id": f"retrieval:{run_id}",
                "activity_type": "retrieval",
                "name": "acquire_context",
                "parent_activity_id": f"run:{run_id or ''}",
                "status": "partial" if has_success and has_failure else "completed" if has_success or not has_failure else "failed",
                "duration_ms": int((perf_counter() - started_clock) * 1000),
                "retrieval_run_id": retrieval_run_id,
                "candidate_count": sum(result.candidate_count for result in results),
                "selected_count": len(kept),
                "injected_count": len(kept),
                "provider": ",".join(result.provider for result in results),
            },
        )
        return bundle

    async def _invoke_provider(self, name: str, provider: ContextProvider, **kwargs: Any) -> ProviderResult:
        started = perf_counter()
        try:
            value = await (
                provider(**kwargs)
                if callable(provider)
                else provider.retrieve_context(**kwargs)
            )
            if not isinstance(value, ProviderResult):
                return ProviderResult(name, "failed", error_message="invalid provider result", duration_ms=(perf_counter() - started) * 1000)
            return ProviderResult(
                value.provider or name,
                value.status,
                value.items,
                value.error_message,
                value.candidate_count,
                value.result_count,
                value.duration_ms or (perf_counter() - started) * 1000,
            )
        except Exception as exc:
            return ProviderResult(name, "failed", error_message=str(exc), duration_ms=(perf_counter() - started) * 1000)

    async def _publish_event(self, event_type: str, session_id: str, run_id: str | None, payload: dict[str, Any]) -> None:
        if self._events is None:
            return
        try:
            from domain.events import ApplicationEvent, EventDurability
            await self._events.publish(ApplicationEvent(
                event_type=event_type,
                durability=EventDurability.DURABLE,
                session_id=session_id,
                run_id=run_id,
                payload=payload,
            ))
        except Exception:
            return


__all__ = ["ContextAcquisitionService"]
