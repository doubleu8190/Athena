"""当前会话任务回顾的有界词法检索。"""

from __future__ import annotations

import asyncio
import logging
import time

from application.runs.context.contracts import ContextItem, ContextPlan, ProviderResult
from application.runs.context.protocols import ContextProvider
from domain.memory import (
    MemoryEpisodeRepositoryPort,
    MemoryEpisodeSearchRequest,
    MemorySourceReference,
    SessionMemorySettingsPort,
)
from domain.retrieval.trace_contracts import (
    RetrievalCandidateTrace,
    RetrievalChannelTrace,
)

from .query_terms import extract_memory_query_terms

logger = logging.getLogger(__name__)


def _score(content: str, query: str, terms: tuple[str, ...]) -> tuple[float, tuple[str, ...]]:
    """按命中词项比例和完整查询命中计算轻量相关度。"""
    normalized = content.casefold()
    matched = tuple(term for term in terms if term.casefold() in normalized)
    score = len(matched) / max(1, len(terms))
    if query.casefold() in normalized:
        score += 1.0
    return score, matched


class MemoryEpisodeContextProvider(ContextProvider):
    """从当前会话中检索过去任务的结构化回顾。"""

    name = "episode"

    def __init__(
        self,
        repository: MemoryEpisodeRepositoryPort,
        *,
        limit: int = 5,
        timeout_seconds: float = 3.0,
        settings: SessionMemorySettingsPort | None = None,
    ) -> None:
        """保存回顾检索依赖。

        参数：repository 执行会话隔离搜索；limit 限制返回项数；timeout_seconds 限制
        仓储等待；settings 在关闭记忆读取时阻止本通道返回资料。
        返回：无。
        异常：不主动抛出业务异常。
        """
        self._repository = repository
        self._limit = limit
        self._timeout_seconds = timeout_seconds
        self._settings = settings

    async def retrieve_context(
        self,
        *,
        session_id: str,
        run_id: str,
        plan: ContextPlan,
    ) -> ProviderResult:
        """只搜索本会话任务回顾并生成候选和选择轨迹。

        参数：session_id 限定可读范围；run_id 由 Provider 协议提供；plan 携带
        查询和共享资料预算。
        返回：带有有界摘要、候选轨迹和词法通道状态的 ProviderResult。
        异常：仓储超时由上层采集服务标记为 timeout；其他异常由上层记录为 failed。
        """
        del run_id
        started = time.perf_counter()
        query = plan.episode_query
        if not query:
            return ProviderResult(self.name, "succeeded", query=query)
        settings = (
            await self._settings.get(session_id)
            if self._settings is not None
            else None
        )
        if settings is not None and not settings.use_memories:
            return ProviderResult(
                self.name,
                "skipped",
                query=query,
                resource_scope={"scope": "session", "memory_suppressed": True},
            )

        terms = extract_memory_query_terms(query)
        logger.info(
            "memory_episode_retrieval_started",
            extra={
                "session_id": session_id,
                "query": query,
                "terms": terms,
            },
        )
        project_id = None if settings is None else settings.project_id
        recall_limit = min(100, max(self._limit, plan.recall_limit) * 8)
        async with asyncio.timeout(self._timeout_seconds):
            episodes = await self._repository.search(
                session_id,
                MemoryEpisodeSearchRequest(
                    query=query,
                    terms=terms,
                    limit=max(1, recall_limit),
                    project_id=project_id,
                ),
            )
        ranked = sorted(
            (
                (*_score(episode.content, query, terms), episode)
                for episode in episodes
            ),
            key=lambda item: (item[0], item[2].created_at),
            reverse=True,
        )
        selected_limit = min(max(0, self._limit), max(0, plan.max_items))
        selected = ranked[:selected_limit]
        selected_ids = {episode.episode_id for _, _, episode in selected}
        expanded_sources: dict[str, tuple[MemorySourceReference, ...]] = {}
        source_expansion_status = "skipped"
        if plan.episode_include_sources and selected:
            try:
                async with asyncio.timeout(
                    max(0.001, self._timeout_seconds - (time.perf_counter() - started))
                ):
                    expanded = await asyncio.gather(
                        *(
                            self._repository.expand_sources(
                                episode.episode_id,
                                session_id=session_id,
                                project_id=project_id,
                                limit=4,
                            )
                            for _, _, episode in selected
                        )
                    )
                expanded_sources = {
                    episode.episode_id: sources
                    for (_, _, episode), sources in zip(selected, expanded, strict=True)
                }
                source_expansion_status = "succeeded"
            except TimeoutError:
                source_expansion_status = "timeout"
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Episode source expansion failed; session_id=%s",
                    session_id,
                )
                source_expansion_status = "failed"
        candidates: list[RetrievalCandidateTrace] = []
        items: list[ContextItem] = []
        for rank, (score, matched, episode) in enumerate(ranked, 1):
            is_selected = episode.episode_id in selected_ids
            summary = episode.summary
            metadata = {
                "project_id": episode.project_id,
                "turn_id": episode.turn_id,
                "outcome": summary.outcome,
                "source_ref_count": len(episode.source_refs),
                "source_types": sorted(
                    {reference.source_type for reference in episode.source_refs}
                ),
                "created_at": episode.created_at,
                "matched_terms": list(matched),
            }
            candidates.append(
                RetrievalCandidateTrace(
                    provider=self.name,
                    source_type="memory_episode",
                    source_id=episode.episode_id,
                    logical_source_id=episode.turn_id,
                    native_rank=rank,
                    native_score=score,
                    fused_rank=rank,
                    fused_score=score,
                    selected_for_result=is_selected,
                    filter_reason=None if is_selected else "provider_top_k",
                    content_preview=episode.content[:500],
                    source_title=summary.title,
                    metadata=metadata,
                    selection_stage=None if is_selected else "provider",
                )
            )
            if is_selected:
                source_refs = expanded_sources.get(episode.episode_id, ())
                source_content = "\n".join(
                    f"[{reference.source_type} | {reference.observed_at}] "
                    f"{reference.excerpt[:1600]}"
                    for reference in source_refs
                )
                content = episode.content
                if source_content:
                    content = f"{content}\n\n按需展开的来源证据（可能截断）：\n{source_content}"
                items.append(
                    ContextItem(
                        provider=self.name,
                        content=content[:12000],
                        source_id=episode.episode_id,
                        metadata={
                            **metadata,
                            "session_id": session_id,
                            "expanded_source_count": len(source_refs),
                            "source_expansion_requested": plan.episode_include_sources,
                            "source_ref_ids": [
                                reference.source_id for reference in episode.source_refs
                            ],
                        },
                    )
                )

        return ProviderResult(
            provider=self.name,
            status="succeeded",
            items=tuple(items),
            candidate_count=len(ranked),
            result_count=len(items),
            duration_ms=round((time.perf_counter() - started) * 1000),
            candidates=tuple(candidates),
            query=query,
            resource_scope={
                "scope": "session_and_project" if project_id else "session",
                "project_bound": project_id is not None,
                "cross_session": project_id is not None,
            },
            effective_params={
                "strategy": "bounded_lexical",
                "recall_limit": recall_limit,
                "result_limit": selected_limit,
                "term_count": len(terms),
                "include_sources": plan.episode_include_sources,
                "memory_token_budget": plan.memory_token_budget,
            },
            channels=(
                RetrievalChannelTrace(
                    channel="keyword",
                    status="succeeded",
                    hit_count=len(ranked),
                ),
                *(
                    (
                        RetrievalChannelTrace(
                            channel="source_expansion",
                            status=source_expansion_status,
                            hit_count=sum(len(refs) for refs in expanded_sources.values()),
                        ),
                    )
                    if plan.episode_include_sources
                    else ()
                ),
            ),
        )


__all__ = ["MemoryEpisodeContextProvider"]
