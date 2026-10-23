from __future__ import annotations

import asyncio
from time import perf_counter
from collections.abc import Callable
from typing import Any

from domain.events import EventPort
from domain.retrieval.query_ports import RetrievalPort
from .protocols import ContextProvider

from .contracts import ContextBundle, ContextItem, ContextPlan, ProviderResult


class ContextAcquisitionService:
    """Run selected context providers concurrently under one budget."""

    def __init__(
        self,
        providers: dict[str, ContextProvider],
        trace_repository: RetrievalPort,
        event_publisher: EventPort,
        token_counter: Callable[[str], int] | None = None,
    ) -> None:
        self._providers = dict(providers)
        self._token_counter = token_counter or (lambda value: len(value.split()))
        self._trace_repository = trace_repository
        self._events = event_publisher

    async def retrieve_context(
        self,
        *,
        session_id: str,
        run_id: str,
        plan: ContextPlan,
        invocation_id: str,
    ) -> ContextBundle:
        """并发执行计划中的检索，并记录本次调用的结果和活动。
        Args:
            session_id: 当前会话 ID。
            run_id: 当前 Root Run ID。
            plan: 已通过资源范围校验的 Provider 查询计划。
            invocation_id: 本次检索的唯一活动 ID。
        Returns:
            按本次计划限制裁剪的 ContextBundle。
        Raises:
            asyncio.CancelledError: 调用方取消检索时原样传播。
        """
        started_clock = perf_counter()
        activity_id = f"retrieval:{invocation_id}"
        await self._publish_event(
            "retrieval.started",
            session_id,
            run_id,
            self._activity_payload(activity_id, run_id, status="running"),
        )
        results = await self._collect_provider_results(
            session_id=session_id,
            run_id=run_id,
            plan=plan,
            activity_id=activity_id,
            started_clock=started_clock,
        )
        bundle = self._build_bundle(results, plan)
        retrieval_run_id = await self._record_trace(
            session_id=session_id,
            run_id=run_id,
            plan=plan,
            bundle=bundle,
        )
        await self._publish_provider_results(
            results,
            session_id=session_id,
            run_id=run_id,
            activity_id=activity_id,
            retrieval_run_id=retrieval_run_id,
        )
        await self._publish_completion(
            results,
            session_id=session_id,
            run_id=run_id,
            activity_id=activity_id,
            retrieval_run_id=retrieval_run_id,
            duration_ms=int((perf_counter() - started_clock) * 1000),
            item_count=len(bundle.items),
        )
        return bundle

    async def _collect_provider_results(
        self,
        *,
        session_id: str,
        run_id: str,
        plan: ContextPlan,
        activity_id: str,
        started_clock: float,
    ) -> list[ProviderResult]:
        """并发调用已选通道，并在取消时等待所有 Provider 收尾。"""
        unknown = [
            name for name in plan.providers if name not in self._providers
        ]
        results = [
            ProviderResult(name, "skipped", error_message="provider unavailable")
            for name in unknown
        ]
        tasks = [
            asyncio.create_task(
                self._invoke_provider(
                    name,
                    self._providers[name],
                    session_id=session_id,
                    run_id=run_id,
                    plan=plan,
                )
            )
            for name in plan.providers
            if name in self._providers
        ]
        try:
            results.extend(await asyncio.gather(*tasks))
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._publish_event(
                "retrieval.failed",
                session_id,
                run_id,
                {
                    **self._activity_payload(
                        activity_id, run_id, status="cancelled"
                    ),
                    "duration_ms": int(
                        (perf_counter() - started_clock) * 1000
                    ),
                    "error": {"code": "retrieval_cancelled"},
                },
            )
            raise
        return results

    def _build_bundle(
        self, results: list[ProviderResult], plan: ContextPlan
    ) -> ContextBundle:
        """按条目数与 token 预算裁剪本次检索结果。"""
        items = [item for result in results for item in result.items]
        truncated = len(items) > plan.max_items
        selected = items[: plan.max_items]
        kept: list[ContextItem] = []
        tokens = 0
        for item in selected:
            count = self._token_counter(item.content)
            if tokens + count > plan.max_tokens:
                truncated = True
                break
            kept.append(item)
            tokens += count
        return ContextBundle(
            items=tuple(kept),
            provider_results=tuple(results),
            providers_succeeded=tuple(
                result.provider
                for result in results
                if result.status == "succeeded"
            ),
            providers_failed=tuple(
                result.provider
                for result in results
                if result.status in {"failed", "timeout"}
            ),
            truncated=truncated,
        )

    async def _record_trace(
        self,
        *,
        session_id: str,
        run_id: str,
        plan: ContextPlan,
        bundle: ContextBundle,
    ) -> str | None:
        """保存实际查询及裁剪后的结果；轨迹故障不覆盖检索结果。"""
        try:
            return await self._trace_repository.record_context(
                session_id=session_id,
                agent_run_id=run_id,
                query=_plan_query_summary(plan),
                provider_results=list(bundle.provider_results),
                items=list(bundle.items),
            )
        except Exception:
            return None

    async def _publish_provider_results(
        self,
        results: list[ProviderResult],
        *,
        session_id: str,
        run_id: str,
        activity_id: str,
        retrieval_run_id: str | None,
    ) -> None:
        """为本次调用的每个通道发布独立结果活动。"""
        for result in results:
            failed = result.status in {"failed", "timeout"}
            await self._publish_event(
                "retrieval.provider.failed"
                if failed
                else "retrieval.provider.completed",
                session_id,
                run_id,
                {
                    "activity_id": f"{activity_id}:provider:{result.provider}",
                    "activity_type": "retrieval_provider",
                    "name": result.provider,
                    "provider": result.provider,
                    "parent_activity_id": activity_id,
                    "retrieval_run_id": retrieval_run_id,
                    "status": "failed" if failed else result.status,
                    "candidate_count": result.candidate_count,
                    "result_count": result.result_count,
                    "duration_ms": int(result.duration_ms),
                },
            )

    async def _publish_completion(
        self,
        results: list[ProviderResult],
        *,
        session_id: str,
        run_id: str,
        activity_id: str,
        retrieval_run_id: str | None,
        duration_ms: int,
        item_count: int,
    ) -> None:
        """按 Provider 汇总状态并结束本次检索活动。"""
        has_success = any(result.status == "succeeded" for result in results)
        has_failure = any(
            result.status in {"failed", "timeout"} for result in results
        )
        event_type = (
            "retrieval.completed" if has_success or not has_failure else "retrieval.failed"
        )
        status = (
            "partial"
            if has_success and has_failure
            else "completed"
            if has_success or not has_failure
            else "failed"
        )
        await self._publish_event(
            event_type,
            session_id,
            run_id,
            {
                **self._activity_payload(activity_id, run_id, status=status),
                "duration_ms": duration_ms,
                "retrieval_run_id": retrieval_run_id,
                "candidate_count": sum(
                    result.candidate_count for result in results
                ),
                "selected_count": item_count,
                "injected_count": item_count,
                "provider": ",".join(result.provider for result in results),
            },
        )

    @staticmethod
    def _activity_payload(
        activity_id: str, run_id: str, *, status: str
    ) -> dict[str, str]:
        """构造 Root 检索活动的关联字段。"""
        return {
            "activity_id": activity_id,
            "activity_type": "retrieval",
            "name": "acquire_context",
            "parent_activity_id": f"run:{run_id or ''}",
            "status": status,
        }

    async def _invoke_provider(
        self,
        name: str,
        provider: ContextProvider,
        *,
        session_id: str,
        run_id: str,
        plan: ContextPlan,
    ) -> ProviderResult:
        """执行一个 Provider，并把普通故障整理成该通道的失败结果。"""
        started = perf_counter()
        try:
            value = await provider.retrieve_context(
                session_id=session_id,
                run_id=run_id,
                plan=plan,
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

    async def _publish_event(
        self,
        event_type: str,
        session_id: str,
        run_id: str,
        payload: dict[str, Any],
    ) -> None:
        """尽力发布检索活动；事件故障不覆盖检索结果。"""
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


def _plan_query_summary(plan: ContextPlan) -> str:
    """从实际执行的通道查询构造检索轨迹摘要。"""
    queries = (
        ("memory", plan.memory_query),
        ("file", plan.file_query),
        ("knowledge", plan.knowledge_query),
        ("graph", plan.graph_query),
    )
    return "\n".join(
        f"{provider}: {query}" for provider, query in queries if query
    )


__all__ = ["ContextAcquisitionService"]
