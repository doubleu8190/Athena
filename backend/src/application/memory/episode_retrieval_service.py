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
    """按命中词项比例和完整查询命中计算轻量相关度。

    参数：
    - `content`（str）：待处理或返回的正文内容；约束：无额外运行时限制。
    - `query`（str）：用于检索、匹配或解析的原始查询文本；约束：无额外运行时限制。
    - `terms`（tuple[str, ...]）：从查询中提取并用于关键词检索的词项；约束：无额外运行时限制。

    返回：
    - tuple[float, tuple[str, ...]]：候选文本的相关性分数及命中的查询词项。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
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

        参数：
        - `repository`（MemoryEpisodeRepositoryPort）：负责读取或持久化领域数据的仓储依赖；约束：无额外运行时限制。
        - `limit`（int）：本次读取或处理的数量上限；约束：可省略，默认值为 `5`。
        - `timeout_seconds`（float）：本次操作允许使用的最长秒数；约束：可省略，默认值为 `3.0`。
        - `settings`（SessionMemorySettingsPort | None）：当前会话的记忆读取设置；None 表示未提供会话级覆盖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
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

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
        - `plan`（ContextPlan）：当前计划对象，提供请求的检索或执行安排；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - ProviderResult：包含词法召回候选、选中回顾摘要及可选来源证据的 Provider 结果。

        异常：
        - asyncio.CancelledError（重新抛出捕获的异常）：校验条件 `plan.episode_include_sources and selected；捕获 asyncio.CancelledError 后` 成立时。
        - 未列出的依赖异常按当前实现传播。
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

        # 用词项提升召回覆盖，再以轻量相关度和创建时间稳定排序候选。
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
            # 来源只为最终选中的回顾展开，并复用检索剩余时限，避免放大查询成本。
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
        # 保留全部候选的选择轨迹，同时只把 top-k 回顾正文及其可选来源加入上下文。
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
