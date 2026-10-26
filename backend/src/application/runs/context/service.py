from __future__ import annotations

import asyncio
from hashlib import sha256
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from uuid import uuid4

from domain.events import EventPort
from domain.memory import (
    MemoryEpisodeRepositoryPort,
    MemoryFactIdentity,
    MemoryRepositoryPort,
    MemorySourceReference,
    MemoryStatus,
)
from domain.retrieval.query_ports import RetrievalPort
from domain.retrieval.trace_contracts import (
    RetrievalCandidateSelection,
    RetrievalCandidateTrace,
    RetrievalProviderTrace,
)

from .contracts import (
    ContextBundle,
    ContextItem,
    ContextPlan,
    ContextQueryOutcome,
    ProviderResult,
    RetrievalStage,
)
from .protocols import ContextProvider
from application.memory.fact_groups import build_fact_context_group, record_fact
from application.memory.fact_policy import cardinality_for
from application.memory.index_views import build_memory_index_item

logger = logging.getLogger(__name__)
_EPISODE_SOURCE_MARKER = "\n\n按需展开的来源证据（可能截断）："


def _strip_episode_sources(item: ContextItem) -> ContextItem:
    """从恢复中的 Root 窗口删除未经本轮复核的原始来源片段。"""
    summary = item.content.split(_EPISODE_SOURCE_MARKER, 1)[0]
    return replace(
        item,
        content=summary,
        metadata={
            **item.metadata,
            "expanded_source_count": 0,
            "source_expansion_revalidated": False,
            "source_ref_ids": [],
        },
    )


def _render_episode_sources(
    item: ContextItem, source_refs: tuple[MemorySourceReference, ...]
) -> ContextItem:
    """用本次重新验证的来源替换旧 checkpoint 中的展开文本。"""
    summary = item.content.split(_EPISODE_SOURCE_MARKER, 1)[0]
    source_content = "\n".join(
        f"[{reference.source_type} | {reference.observed_at}] "
        f"{reference.excerpt[:1600]}"
        for reference in source_refs
    )
    content = (
        f"{summary}{_EPISODE_SOURCE_MARKER}\n{source_content}"
        if source_content
        else summary
    )
    return replace(
        item,
        content=content[:12000],
        metadata={
            **item.metadata,
            "expanded_source_count": len(source_refs),
            "source_expansion_revalidated": True,
            "source_ref_ids": [reference.source_id for reference in source_refs],
        },
    )


def _memory_record_is_current(record: Any) -> bool:
    """Fail closed on inactive, uncertain, expired, or future-effective memory."""
    if record.status != MemoryStatus.ACTIVE or record.validity_status != "valid":
        return False
    now = datetime.now(timezone.utc)
    for value in (record.expires_at, record.valid_until):
        if value is None:
            continue
        try:
            deadline = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline <= now:
            return False
    fact = record_fact(record)
    if fact is not None and fact.valid_from is not None:
        try:
            valid_from = datetime.fromisoformat(fact.valid_from)
        except (TypeError, ValueError):
            return False
        if valid_from.tzinfo is None:
            valid_from = valid_from.replace(tzinfo=timezone.utc)
        if valid_from > now:
            return False
    return True


def _without_unvalidated_memory(bundle: ContextBundle) -> ContextBundle:
    """Remove unverified memory and record why selected candidates were dropped."""
    selections = tuple(
        selection.model_copy(
            update={
                "context_selected": False,
                "source_ids": (),
                "filter_reason": "memory_final_validation_failed",
            }
        )
        if selection.provider in {"memory", "memory_index"} and selection.context_selected
        else selection
        for selection in bundle.context_selection
    )
    return replace(
        bundle,
        items=tuple(
            _strip_episode_sources(item)
            for item in bundle.items
            if item.provider not in {"memory", "memory_index"}
        ),
        context_selection=selections,
    )


class ContextAcquisitionService:
    """并发获取上下文资料，并保存本次召回分析。"""

    def __init__(
        self,
        providers: dict[str, ContextProvider],
        trace_repository: RetrievalPort,
        event_publisher: EventPort,
        memory_repository: MemoryRepositoryPort | None = None,
        episode_repository: MemoryEpisodeRepositoryPort | None = None,
        token_counter: Callable[[str], int] | None = None,
    ) -> None:
        self._providers = dict(providers)
        self._token_counter = token_counter or (lambda value: len(value.split()))
        self._trace_repository = trace_repository
        self._memory_repository = memory_repository
        self._episode_repository = episode_repository
        self._events = event_publisher

    async def validate_memory_context(
        self, bundle: ContextBundle, *, timeout_seconds: float = 1.0
    ) -> ContextBundle:
        """在选择落库和模型调用前，重新确认记忆仍然有效且冲突组完整。"""
        memory_items = [item for item in bundle.items if item.provider == "memory"]
        index_items = [item for item in bundle.items if item.provider == "memory_index"]
        episode_items = [
            item
            for item in bundle.items
            if item.provider == "episode"
            and item.metadata.get("source_expansion_requested") is True
        ]
        if not memory_items and not index_items and not episode_items:
            return bundle
        repository = self._memory_repository
        validated_by_alias: dict[str, ContextItem] = {}
        if repository is None and (memory_items or index_items):
            logger.warning("Memory context cannot be revalidated because the repository is unavailable")
            return _without_unvalidated_memory(bundle)
        episode_repository = self._episode_repository

        async def validate_one(item: ContextItem) -> ContextItem | None:
            try:
                return await self._validate_memory_item(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Failed to revalidate memory context before model use; memory_id=%s",
                    item.source_id,
                )
                return None

        semaphore = asyncio.Semaphore(4)

        async def bounded_validate(item: ContextItem) -> ContextItem | None:
            async with semaphore:
                return await validate_one(item)

        async def bounded_validate_index(item: ContextItem) -> ContextItem | None:
            async with semaphore:
                try:
                    session_id = item.metadata.get("session_id")
                    project_id = item.metadata.get("project_id")
                    if not isinstance(session_id, str) or not session_id:
                        return None
                    if project_id is not None and not isinstance(project_id, str):
                        return None
                    records = await repository.list_scope_index(
                        session_id=session_id,
                        project_id=project_id,
                        limit=80,
                    )
                    return await build_memory_index_item(
                        repository,
                        records,
                        session_id=session_id,
                        project_id=project_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Failed to revalidate memory index before model use; source_id=%s",
                        item.source_id,
                    )
                    return None

        async def bounded_revalidate_episode(item: ContextItem) -> ContextItem:
            async with semaphore:
                return await self._revalidate_episode_sources(
                    item, episode_repository
                )

        try:
            async with asyncio.timeout(max(0.01, timeout_seconds)):
                validated = await asyncio.gather(
                    *(bounded_validate(item) for item in memory_items)
                )
                validated_indexes = await asyncio.gather(
                    *(bounded_validate_index(item) for item in index_items)
                )
                validated_episodes = await asyncio.gather(
                    *(bounded_revalidate_episode(item) for item in episode_items)
                )
        except TimeoutError:
            logger.warning("Memory context revalidation timed out; dropping memory context")
            return _without_unvalidated_memory(bundle)

        validated_episode_by_id = {
            original.source_id: current
            for original, current in zip(episode_items, validated_episodes, strict=True)
            if current is not None and original.source_id is not None
        }

        validated_index_by_id = {
            original.source_id: current
            for original, current in zip(index_items, validated_indexes, strict=True)
            if current is not None and original.source_id is not None
        }

        for original, current in zip(memory_items, validated, strict=True):
            if current is None:
                continue
            aliases = {
                original.source_id,
                *(
                    str(value)
                    for value in original.metadata.get("memory_member_ids", ())
                    if isinstance(value, str)
                ),
            }
            for alias in aliases:
                if alias:
                    validated_by_alias[alias] = current

        validated_items: list[ContextItem] = []
        memory_index = 0
        for item in bundle.items:
            if (
                item.provider == "episode"
                and item.metadata.get("source_expansion_requested") is True
            ):
                validated_items.append(
                    validated_episode_by_id.get(item.source_id, _strip_episode_sources(item))
                )
                continue
            if item.provider == "memory_index":
                current_index = validated_index_by_id.get(item.source_id)
                if current_index is not None:
                    validated_items.append(current_index)
                continue
            if item.provider != "memory":
                validated_items.append(item)
                continue
            current = validated[memory_index]
            memory_index += 1
            if current is not None:
                validated_items.append(current)

        selections: list[RetrievalCandidateSelection] = []
        for selection in bundle.context_selection:
            if selection.provider == "memory_index" and selection.context_selected:
                current_index = validated_index_by_id.get(selection.source_id)
                if current_index is None:
                    selections.append(
                        selection.model_copy(
                            update={
                                "context_selected": False,
                                "source_ids": (),
                                "filter_reason": "memory_final_validation_failed",
                            }
                        )
                    )
                else:
                    selections.append(selection)
                continue
            if selection.provider != "memory" or not selection.context_selected:
                selections.append(selection)
                continue
            current = next(
                (
                    validated_by_alias[memory_id]
                    for memory_id in (selection.source_ids or (selection.source_id,))
                    if memory_id in validated_by_alias
                ),
                None,
            )
            if current is None:
                selections.append(
                    selection.model_copy(
                        update={
                            "context_selected": False,
                            "source_ids": (),
                            "filter_reason": "memory_final_validation_failed",
                        }
                    )
                )
                continue
            current_ids = current.metadata.get("memory_member_ids", ())
            selections.append(
                selection.model_copy(
                    update={
                        "source_id": current.source_id or selection.source_id,
                        "source_ids": tuple(current_ids)
                        if current_ids
                        else ((current.source_id,) if current.source_id else ()),
                        "context_selected": True,
                        "filter_reason": None,
                    }
                )
            )
        return replace(
            bundle,
            items=tuple(validated_items),
            context_selection=tuple(selections),
        )

    async def _revalidate_episode_sources(
        self,
        item: ContextItem,
        repository: MemoryEpisodeRepositoryPort | None,
    ) -> ContextItem:
        """在每次模型调用前重新读取已展开的有限回顾来源。"""
        if repository is None or item.source_id is None:
            return _strip_episode_sources(item)
        session_id = item.metadata.get("session_id")
        project_id = item.metadata.get("project_id")
        if not isinstance(session_id, str) or not session_id:
            return _strip_episode_sources(item)
        if project_id is not None and not isinstance(project_id, str):
            return _strip_episode_sources(item)
        try:
            source_refs = await repository.expand_sources(
                item.source_id,
                session_id=session_id,
                project_id=project_id,
                limit=4,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Failed to revalidate episode sources before model use; episode_id=%s",
                item.source_id,
            )
            return _strip_episode_sources(item)
        return _render_episode_sources(item, source_refs)

    async def _validate_memory_item(self, item: ContextItem) -> ContextItem | None:
        repository = self._memory_repository
        if repository is None:
            return None
        identity_value = item.metadata.get("memory_fact_identity")
        identity: MemoryFactIdentity | None = None
        if identity_value is not None:
            try:
                identity = MemoryFactIdentity.model_validate(identity_value)
            except (TypeError, ValueError):
                identity = None

        seed = None
        if identity is None and item.source_id:
            seed = await repository.get(item.source_id)
            fact = record_fact(seed) if seed is not None else None
            if fact is not None:
                identity = MemoryFactIdentity(
                    scope_kind=fact.scope_kind,
                    scope_id=fact.scope_id,
                    subject_key=fact.subject_key,
                    fact_key=fact.fact_key,
                )
        if identity is None:
            if seed is None or not _memory_record_is_current(seed):
                return None
            return replace(item, content=seed.content, source_id=seed.id)

        related = await repository.list_fact_group_by_identity(identity)
        related = [record for record in related if _memory_record_is_current(record)]
        member_value = item.metadata.get("memory_group_member_value")
        cardinality = str(item.metadata.get("fact_cardinality") or "")
        if not cardinality:
            cardinality = cardinality_for(identity.fact_key).value
        if cardinality == "set" and isinstance(member_value, str):
            related = [
                record
                for record in related
                if (fact := record_fact(record)) is not None
                and fact.fact_value == member_value
            ]
        if not related:
            return None
        seed = next((record for record in related if record.id == item.source_id), None)
        if seed is None:
            seed = related[0]
        group = build_fact_context_group(seed, related)
        if group is None:
            return None
        metadata = {
            **item.metadata,
            "memory_group_key": group.group_key,
            "memory_member_ids": list(group.member_ids),
            "memory_conflict": group.conflicted,
            "memory_total_count": group.total_member_count,
            "memory_complete": group.complete,
            "fact_cardinality": group.cardinality.value,
            "memory_fact_identity": group.identity.model_dump(mode="json"),
            "memory_group_member_value": group.member_value,
        }
        return replace(
            item,
            content=group.content,
            source_id=group.anchor_id,
            metadata=metadata,
        )

    async def retrieve_context(
        self,
        *,
        session_id: str,
        run_id: str,
        plan: ContextPlan,
        invocation_id: str,
        stage: RetrievalStage,
        message_id: str | None = None,
        skipped_outcomes: tuple[ContextQueryOutcome, ...] = (),
    ) -> ContextBundle:
        """执行同一阶段的检索并保存候选。

        参数：session_id/run_id/message_id 关联请求；plan 限定查询和规模；
        invocation_id 标识本批调用；stage 由工作流指定预检索或定向检索。
        返回：ContextBundle，包含已裁剪资料和通道摘要。
        异常：外部取消时传播 CancelledError；普通通道故障写入结果。
        """
        started_clock = perf_counter()
        activity_id = f"retrieval:{invocation_id}"
        await self._publish_event(
            "retrieval.started",
            session_id,
            run_id,
            self._activity_payload(activity_id, run_id, stage=stage, status="running"),
            transition_id=f"{activity_id}:started",
        )
        await self._publish_skipped_provider_outcomes(
            skipped_outcomes,
            session_id=session_id,
            run_id=run_id,
            activity_id=activity_id,
        )
        results, cancelled = await self._collect_provider_results(
            session_id=session_id,
            run_id=run_id,
            plan=plan,
            activity_id=activity_id,
            stage=stage,
        )
        # 阶段归因由程序统一覆盖，Provider 只负责它自己的检索事实。
        results = [
            replace(
                result,
                effective_params={**result.effective_params, "retrieval_stage": stage},
            )
            for result in results
        ]
        bundle = self._build_bundle(results, plan)
        duration_ms = (perf_counter() - started_clock) * 1000
        retrieval_run_id = await self._record_trace(
            invocation_id=invocation_id,
            session_id=session_id,
            run_id=run_id,
            message_id=message_id,
            plan=plan,
            bundle=bundle,
            duration_ms=duration_ms,
            cancelled=cancelled,
        )
        # Root checkpoint 只保留窗口和模块摘要，全部候选已单独写入分析表。
        bundle = replace(
            bundle,
            retrieval_run_id=retrieval_run_id,
            provider_results=tuple(
                replace(result, candidates=()) for result in bundle.provider_results
            ),
        )
        await self._publish_completion(
            results,
            session_id=session_id,
            run_id=run_id,
            activity_id=activity_id,
            retrieval_run_id=retrieval_run_id,
            duration_ms=int(duration_ms),
            bundle=bundle,
            cancelled=cancelled,
            stage=stage,
        )
        if cancelled:
            raise asyncio.CancelledError
        return bundle

    async def publish_skipped_provider_outcomes(
        self,
        outcomes: tuple[ContextQueryOutcome, ...],
        *,
        session_id: str,
        run_id: str,
        stage: RetrievalStage,
    ) -> None:
        """Publish truthful skip facts when a retrieval stage has no active provider."""
        activity_id = f"retrieval:{run_id}:{stage}:skipped"
        await self._publish_skipped_provider_outcomes(
            outcomes,
            session_id=session_id,
            run_id=run_id,
            activity_id=activity_id,
        )

    async def confirm_selection(
        self,
        *,
        run_id: str | None,
        session_id: str,
        agent_run_id: str,
        selections: tuple[RetrievalCandidateSelection, ...],
        stage: RetrievalStage,
    ) -> None:
        """记录 Root 最终窗口选择，并只为本轮保留的记忆计数。"""
        memory_ids = list(
            dict.fromkeys(
                memory_id
                for selection in selections
                if selection.provider == "memory" and selection.context_selected
                for memory_id in (selection.source_ids or (selection.source_id,))
            )
        )
        if memory_ids and self._memory_repository is not None:
            try:
                await self._memory_repository.record_access(memory_ids)
            except Exception:
                logger.exception(
                    "Failed to update access counts for selected memories; retrieval_run_id=%s",
                    run_id,
                )

        if run_id is None:
            if not selections:
                return
            await self._publish_selection_event(
                session_id=session_id,
                agent_run_id=agent_run_id,
                run_id=None,
                selections=selections,
                persisted=False,
                stage=stage,
            )
            return
        persisted = False
        try:
            persisted = await self._trace_repository.update_context_selection(
                run_id=run_id,
                selections=selections,
            )
            if not persisted:
                logger.error(
                    "Retrieval run was not found while saving final context selection; retrieval_run_id=%s",
                    run_id,
                )
        except Exception:
            logger.exception(
                "Failed to save final retrieval context selection; retrieval_run_id=%s",
                run_id,
            )
        await self._publish_selection_event(
            session_id=session_id,
            agent_run_id=agent_run_id,
            run_id=run_id,
            selections=selections,
            persisted=persisted,
            stage=stage,
        )

    async def persist_revalidated_selection(self, bundle: ContextBundle) -> None:
        """同步最终模型调用前的筛选结果，不重复增加记忆访问次数。"""
        if bundle.retrieval_run_id is None or not bundle.context_selection:
            return
        try:
            persisted = await self._trace_repository.update_context_selection(
                run_id=bundle.retrieval_run_id,
                selections=bundle.context_selection,
            )
            if not persisted:
                logger.warning(
                    "Retrieval run was not found while persisting revalidated context selection; retrieval_run_id=%s",
                    bundle.retrieval_run_id,
                )
        except Exception:
            logger.exception(
                "Failed to persist revalidated context selection; retrieval_run_id=%s",
                bundle.retrieval_run_id,
            )

    async def _publish_selection_event(
        self,
        *,
        session_id: str,
        agent_run_id: str,
        run_id: str | None,
        selections: tuple[RetrievalCandidateSelection, ...],
        persisted: bool,
        stage: RetrievalStage,
    ) -> None:
        invocation_id = (
            run_id.removeprefix("retrieval_") if run_id is not None else "unknown"
        )
        await self._publish_event(
            "retrieval.selection.completed",
            session_id,
            agent_run_id,
            {
                "activity_id": f"retrieval:{invocation_id}:selection",
                "activity_type": "retrieval_selection",
                "name": "context_window_selection",
                "parent_activity_id": f"retrieval:{invocation_id}",
                "retrieval_run_id": run_id,
                "stage": stage,
                "trace_status": "complete" if persisted else "incomplete",
                "selection_status": "complete" if persisted else "pending",
                "context_count": sum(
                    selection.context_selected for selection in selections
                ),
            },
        )

    async def _collect_provider_results(
        self,
        *,
        session_id: str,
        run_id: str,
        plan: ContextPlan,
        activity_id: str,
        stage: RetrievalStage,
    ) -> tuple[list[ProviderResult], bool]:
        """并发调用已选模块；取消时保留已经完成的模块结果。"""
        unknown = [name for name in plan.providers if name not in self._providers]
        results = [
            ProviderResult(name, "skipped", error_message="provider unavailable")
            for name in unknown
        ]
        for result in results:
            await self._publish_provider_results(
                [result],
                session_id=session_id,
                run_id=run_id,
                activity_id=activity_id,
                retrieval_run_id=None,
                stage=stage,
                plan=plan,
            )

        queries = {
            "memory": plan.memory_query,
            "episode": plan.episode_query,
            "knowledge": plan.knowledge_query,
            "file": plan.file_query,
            "graph": plan.graph_query,
        }
        provider_names = [name for name in plan.providers if name in self._providers]
        for name in provider_names:
            scope = {
                "knowledge_base_ids": list(plan.knowledge_base_ids),
                "file_ids": list(plan.file_ids),
            }
            await self._publish_event(
                "retrieval.provider.started",
                session_id,
                run_id,
                {
                    "activity_id": f"{activity_id}:provider:{name}",
                    "activity_type": "retrieval_provider",
                    "name": name,
                    "provider": name,
                    "stage": stage,
                    "parent_activity_id": activity_id,
                    "status": "running",
                    **({"query": queries[name][:500]} if queries.get(name) else {}),
                    **(
                        {"resource_scope": scope}
                        if name in {"knowledge", "file"}
                        else {}
                    ),
                },
                transition_id=f"{activity_id}:provider:{name}:started",
            )
        tasks = {
            asyncio.create_task(
                self._invoke_provider(
                    name,
                    self._providers[name],
                    session_id=session_id,
                    run_id=run_id,
                    plan=plan,
                )
            ): name
            for name in provider_names
        }
        completed_results: dict[asyncio.Task[ProviderResult], ProviderResult] = {}
        try:
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    result = task.result()
                    completed_results[task] = result
                    results.append(result)
                    await self._publish_provider_results(
                        [result],
                        session_id=session_id,
                        run_id=run_id,
                        activity_id=activity_id,
                        retrieval_run_id=None,
                        stage=stage,
                        plan=plan,
                    )
            return results, False
        except asyncio.CancelledError:
            logger.info("Context retrieval cancelled.")
            for task in tasks:
                # 先取消还没有完成的任务
                if not task.done():
                    task.cancel()
            settled = await asyncio.gather(*tasks, return_exceptions=True)
            for task, value in zip(tasks, settled, strict=True):
                if task in completed_results:
                    continue
                if isinstance(value, ProviderResult):
                    results.append(value)
                    await self._publish_provider_results(
                        [value],
                        session_id=session_id,
                        run_id=run_id,
                        activity_id=activity_id,
                        retrieval_run_id=None,
                        stage=stage,
                        plan=plan,
                    )
                elif isinstance(value, BaseException) and not isinstance(
                    value, asyncio.CancelledError
                ):
                    # 处理异常但是不是因为取消信号导致异常的任务
                    result = ProviderResult(
                        tasks[task], "failed", error_message=type(value).__name__
                    )
                    results.append(result)
                    await self._publish_provider_results(
                        [result],
                        session_id=session_id,
                        run_id=run_id,
                        activity_id=activity_id,
                        retrieval_run_id=None,
                        stage=stage,
                        plan=plan,
                    )
                else:
                    # 处理取消的任务
                    result = ProviderResult(
                        tasks[task], "cancelled", error_message="retrieval cancelled"
                    )
                    results.append(result)
                    await self._publish_provider_results(
                        [result],
                        session_id=session_id,
                        run_id=run_id,
                        activity_id=activity_id,
                        retrieval_run_id=None,
                        stage=stage,
                        plan=plan,
                    )
            return results, True

    def _build_bundle(
        self, results: list[ProviderResult], plan: ContextPlan
    ) -> ContextBundle:
        """按现有条数和 token 规则裁剪，并给淘汰候选标原因。"""
        all_items = [item for result in results for item in result.items]
        limit_rejected = all_items[max(0, plan.max_items) :]
        selected = all_items[: max(0, plan.max_items)]
        kept: list[ContextItem] = []
        reasons: dict[tuple[str, str], str] = {
            _item_key(item): "bundle_max_items" for item in limit_rejected
        }
        tokens = 0
        memory_tokens = 0
        token_stopped = False
        for item in selected:
            if token_stopped:
                reasons[_item_key(item)] = "token_budget_stop"
                continue
            count = self._token_counter(item.content)
            is_memory = item.provider in {"memory", "memory_index", "episode"}
            if is_memory and memory_tokens + count > plan.memory_token_budget:
                reasons[_item_key(item)] = "memory_token_budget"
                continue
            if tokens + count > plan.max_tokens:
                reasons[_item_key(item)] = "bundle_token_budget"
                token_stopped = True
                continue
            kept.append(item)
            tokens += count
            if is_memory:
                memory_tokens += count

        kept_keys = {_item_key(item) for item in kept}
        traced_results = [
            _mark_bundle_selection(result, kept_keys, reasons) for result in results
        ]
        truncated = len(all_items) > len(kept)
        return ContextBundle(
            items=tuple(kept),
            provider_results=tuple(traced_results),
            providers_succeeded=tuple(
                result.provider for result in results if result.status == "succeeded"
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
        invocation_id: str,
        session_id: str,
        run_id: str,
        message_id: str | None,
        plan: ContextPlan,
        bundle: ContextBundle,
        duration_ms: float,
        cancelled: bool,
    ) -> str | None:
        """在短事务中保存全部模块候选；保存失败不影响资料返回。"""
        provider_traces = tuple(
            RetrievalProviderTrace(
                provider=result.provider,
                query=result.query or _provider_query(plan, result.provider),
                resource_scope=result.resource_scope,
                effective_params=result.effective_params,
                status=result.status,
                duration_ms=max(0.0, result.duration_ms),
                error_message=result.error_message,
                channels=result.channels,
                candidate_count=result.candidate_count,
                selected_count=result.result_count,
            )
            for result in bundle.provider_results
        )
        candidates = tuple(
            candidate
            for result in bundle.provider_results
            for candidate in result.candidates
        )
        errors = [
            f"{result.provider}: {result.error_message}"
            for result in bundle.provider_results
            if result.error_message
        ]
        try:
            async with asyncio.timeout(2.0):
                return await self._trace_repository.record_context(
                    run_id=f"retrieval_{invocation_id}",
                    session_id=session_id,
                    agent_run_id=run_id,
                    message_id=message_id,
                    query=_plan_query_summary(plan),
                    providers=provider_traces,
                    candidates=candidates,
                    duration_ms=duration_ms,
                    status=_run_status(bundle.provider_results, cancelled=cancelled),
                    error_message="; ".join(errors)[:2000] or None,
                )
        except Exception:
            logger.exception(
                "Failed to persist retrieval analysis; session_id=%s agent_run_id=%s invocation_id=%s",
                session_id,
                run_id,
                invocation_id,
            )
            return None

    async def _publish_provider_results(
        self,
        results: list[ProviderResult],
        *,
        session_id: str,
        run_id: str,
        activity_id: str,
        retrieval_run_id: str | None,
        stage: RetrievalStage,
        plan: ContextPlan,
    ) -> None:
        """为本次调用的每个检索模块发布结果活动。"""
        for result in results:
            failed = result.status in {"failed", "timeout"}
            source_titles = [
                candidate.source_title.strip()[:160]
                for candidate in result.candidates
                if candidate.selected_for_result
                and candidate.source_title
                and candidate.source_title.strip()
            ][:5]
            query_by_provider = {
                "memory": plan.memory_query,
                "episode": plan.episode_query,
                "knowledge": plan.knowledge_query,
                "file": plan.file_query,
                "graph": plan.graph_query,
            }
            resource_scope = (
                {"knowledge_base_ids": list(plan.knowledge_base_ids)}
                if result.provider == "knowledge"
                else (
                    {"file_ids": list(plan.file_ids)}
                    if result.provider == "file"
                    else None
                )
            )
            query = query_by_provider.get(result.provider)
            await self._publish_event(
                (
                    "retrieval.provider.failed"
                    if failed
                    else "retrieval.provider.completed"
                ),
                session_id,
                run_id,
                {
                    "activity_id": f"{activity_id}:provider:{result.provider}",
                    "activity_type": "retrieval_provider",
                    "name": result.provider,
                    "provider": result.provider,
                    "stage": stage,
                    "parent_activity_id": activity_id,
                    "retrieval_run_id": retrieval_run_id,
                    "status": result.status,
                    "candidate_count": result.candidate_count,
                    "result_count": result.result_count,
                    "duration_ms": int(result.duration_ms),
                    "error": result.error_message,
                    "source_titles": source_titles,
                    **({"query": query[:500]} if query else {}),
                    **({"resource_scope": resource_scope} if resource_scope else {}),
                },
                transition_id=f"{activity_id}:provider:{result.provider}:completed",
            )

    async def _publish_skipped_provider_outcomes(
        self,
        outcomes: tuple[ContextQueryOutcome, ...],
        *,
        session_id: str,
        run_id: str,
        activity_id: str,
    ) -> None:
        """Expose skipped provider decisions without presenting them as active work."""
        for outcome in outcomes:
            if outcome.status != "skipped":
                continue
            query = outcome.query
            provider = query.provider
            resource_scope = (
                {
                    "knowledge_base_ids": (
                        list(query.knowledge_base_ids)
                        if query.knowledge_base_ids is not None
                        else None
                    )
                }
                if provider == "knowledge"
                else {"file_ids": list(query.file_ids)} if provider == "file" else None
            )
            provider_activity_id = f"{activity_id}:provider:{provider}:{uuid4().hex}"
            await self._publish_event(
                "retrieval.provider.completed",
                session_id,
                run_id,
                {
                    "activity_id": provider_activity_id,
                    "activity_type": "retrieval_provider",
                    "name": provider,
                    "provider": provider,
                    "stage": outcome.stage,
                    "parent_activity_id": activity_id,
                    "status": "skipped",
                    "result_count": outcome.result_count,
                    "error": outcome.error_message,
                    "query": query.query[:500],
                    **({"resource_scope": resource_scope} if resource_scope else {}),
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
        bundle: ContextBundle,
        cancelled: bool,
        stage: RetrievalStage,
    ) -> None:
        """结束活动并发布实际获得的召回数量。"""
        status = _run_status(results, cancelled=cancelled)
        event_type = (
            "retrieval.failed"
            if status in {"failed", "timeout", "cancelled"}
            else "retrieval.completed"
        )
        await self._publish_event(
            event_type,
            session_id,
            run_id,
            {
                **self._activity_payload(
                    activity_id, run_id, stage=stage, status=status
                ),
                "duration_ms": duration_ms,
                "retrieval_run_id": retrieval_run_id,
                "trace_status": (
                    "pending" if retrieval_run_id is not None else "incomplete"
                ),
                "raw_hit_count": sum(
                    channel.hit_count
                    for result in results
                    for channel in result.channels
                ),
                "candidate_count": sum(result.candidate_count for result in results),
                "selected_count": sum(result.result_count for result in results),
                "bundle_count": len(bundle.items),
                "provider": ",".join(result.provider for result in results),
            },
        )

    @staticmethod
    def _activity_payload(
        activity_id: str, run_id: str, *, stage: RetrievalStage, status: str
    ) -> dict[str, str]:
        """构造 Root 检索活动的关联字段。"""
        return {
            "activity_id": activity_id,
            "activity_type": "retrieval",
            "name": "prefetch_context" if stage == "prefetch" else "acquire_context",
            "stage": stage,
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
        """执行整个通道调用，保持快通道结果并限制预检索等待。

        参数：name/provider 是通道及其实现；session_id/run_id 关联请求；
        plan 的可选上限覆盖枚举文档、关键词和向量查询。
        返回：ProviderResult，超时和普通异常分别标记。
        异常：外部 CancelledError 原样传播。
        """
        started = perf_counter()
        try:
            async with asyncio.timeout(plan.provider_timeout_seconds):
                value = await provider.retrieve_context(
                    session_id=session_id,
                    run_id=run_id,
                    plan=plan,
                )
            if not isinstance(value, ProviderResult):
                return ProviderResult(
                    name,
                    "failed",
                    error_message="invalid provider result",
                    duration_ms=(perf_counter() - started) * 1000,
                )
            return replace(
                value,
                provider=value.provider or name,
                duration_ms=value.duration_ms or (perf_counter() - started) * 1000,
            )
        except TimeoutError:
            return ProviderResult(
                name,
                "timeout",
                error_message="context_provider_timeout",
                duration_ms=(perf_counter() - started) * 1000,
            )
        except Exception as exc:
            return ProviderResult(
                name,
                "failed",
                error_message=str(exc),
                duration_ms=(perf_counter() - started) * 1000,
            )

    async def _publish_event(
        self,
        event_type: str,
        session_id: str,
        run_id: str,
        payload: dict[str, Any],
        *,
        transition_id: str | None = None,
    ) -> None:
        """尽力发布检索活动；事件故障不覆盖检索结果。"""
        try:
            from domain.events import ApplicationEvent, EventDurability

            await self._events.publish(
                ApplicationEvent(
                    event_type=event_type,
                    durability=EventDurability.DURABLE,
                    session_id=session_id,
                    run_id=run_id,
                    transition_id=transition_id,
                    payload=payload,
                )
            )
        except Exception:
            logger.exception(
                "Failed to publish retrieval event; type=%s session_id=%s run_id=%s",
                event_type,
                session_id,
                run_id,
            )


def _item_key(item: ContextItem) -> tuple[str, str]:
    """为资料窗口和候选记录生成相同的模块来源键。"""
    digest = sha256(item.content.encode("utf-8")).hexdigest()[:24]
    group_key = item.metadata.get("memory_group_key")
    source_id = (
        group_key
        if item.provider == "memory" and isinstance(group_key, str) and group_key
        else item.source_id or f"inline_{digest}"
    )
    return item.provider, source_id


def _mark_bundle_selection(
    result: ProviderResult,
    kept_keys: set[tuple[str, str]],
    reasons: dict[tuple[str, str], str],
) -> ProviderResult:
    candidates: list[RetrievalCandidateTrace] = []
    for candidate in result.candidates:
        key = (candidate.provider, candidate.source_id)
        group_key = candidate.metadata.get("memory_group_key")
        group_selected = (
            isinstance(group_key, str)
            and (candidate.provider, group_key) in kept_keys
        )
        if not candidate.selected_for_result:
            candidates.append(candidate)
        elif key in kept_keys or group_selected:
            candidates.append(
                candidate.model_copy(update={"selection_stage": "bundle"})
            )
        else:
            candidates.append(
                candidate.model_copy(
                    update={
                        "filter_reason": candidate.filter_reason
                        or reasons.get(key, "bundle_filtered"),
                        "selection_stage": "bundle",
                    }
                )
            )
    return replace(result, candidates=tuple(candidates))


def _provider_query(plan: ContextPlan, provider: str) -> str | None:
    return {
        "memory": plan.memory_query,
        "episode": plan.episode_query,
        "file": plan.file_query,
        "knowledge": plan.knowledge_query,
        "graph": plan.graph_query,
    }.get(provider)


def _run_status(
    results: list[ProviderResult] | tuple[ProviderResult, ...], *, cancelled: bool
) -> str:
    if cancelled:
        return "cancelled"
    active = [result for result in results if result.status != "skipped"]
    if not active:
        return "skipped"
    succeeded = any(result.status == "succeeded" for result in active)
    failures = [result for result in active if result.status in {"failed", "timeout"}]
    if succeeded and failures:
        return "partial"
    if succeeded:
        return "succeeded"
    if all(result.status == "timeout" for result in active):
        return "timeout"
    if any(result.status == "cancelled" for result in active):
        return "cancelled"
    return "failed"


def _plan_query_summary(plan: ContextPlan) -> str:
    """从各检索模块的实际查询构造召回摘要。"""
    queries = (
        ("memory", plan.memory_query),
        ("episode", plan.episode_query),
        ("file", plan.file_query),
        ("knowledge", plan.knowledge_query),
        ("graph", plan.graph_query),
    )
    return "\n".join(f"{provider}: {query}" for provider, query in queries if query)


__all__ = ["ContextAcquisitionService"]
