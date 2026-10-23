"""记忆上下文检索用例。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from domain.memory import (
    MemoryRepositoryPort,
    MemorySearchRequest,
    MemoryStatus,
    MemoryVectorSearchPort,
)
from domain.retrieval import RetrievalCandidate, RetrievalRankFusion
from domain.retrieval.trace_contracts import (
    RetrievalCandidateTrace,
    RetrievalChannelTrace,
    RetrievalHitTrace,
)

from application.runs.context.contracts import ContextItem, ContextPlan, ProviderResult
from application.runs.context.protocols import ContextProvider


def _keyword_score(content: str, query: str) -> float:
    terms = {term.lower() for term in query.split() if term.strip()}
    if not terms:
        return 0.0
    lowered = content.lower()
    return sum(term in lowered for term in terms) / len(terms)


class MemoryContextProvider(ContextProvider):
    """从记忆端口执行关键词与向量混合召回。"""

    name = "memory"

    def __init__(
        self,
        repository: MemoryRepositoryPort,
        vector_indexer: MemoryVectorSearchPort | None = None,
        *,
        limit: int = 8,
        timeout_seconds: float = 3.0,
    ) -> None:
        self._repository = repository
        self._vector_indexer = vector_indexer
        self._limit = limit
        self._timeout_seconds = timeout_seconds

    async def retrieve_context(
        self,
        *,
        session_id: str,
        run_id: str,
        plan: ContextPlan,
    ) -> ProviderResult:
        started = time.perf_counter()
        query = plan.memory_query
        if not query:
            return ProviderResult(self.name, "succeeded", query=query)

        limit = max(self._limit, plan.recall_limit)
        keyword_rows = []
        vector_rows: list[dict[str, Any]] = []
        keyword_status = "skipped"
        vector_status = "skipped"
        processing_stage = "keyword"
        vector_completed = False
        try:
            async with asyncio.timeout(self._timeout_seconds):
                keyword_rows = await self._repository.search(
                    MemorySearchRequest(query=query, limit=limit)
                )
                keyword_status = "succeeded"
                active_keyword = [
                    (rank, record)
                    for rank, record in enumerate(keyword_rows, 1)
                    if record.status == MemoryStatus.ACTIVE
                ]
                keyword_candidates = [
                    self._candidate(
                        record.id,
                        record.content,
                        record.score or _keyword_score(record.content, query),
                        {
                            **record.metadata,
                            "channel_rank": rank,
                            "channel_score": record.score
                            if record.score is not None
                            else _keyword_score(record.content, query),
                        },
                    )
                    for rank, record in active_keyword
                ]
                processing_stage = "vector"
                if self._vector_indexer is not None:
                    vector_rows = await self._vector_indexer.query(query, limit)
                    vector_status = "succeeded"
                    vector_completed = True
                vector_candidates = self._vector_candidates(vector_rows)

            inactive_ids = {
                record.id
                for record in keyword_rows
                if record.status != MemoryStatus.ACTIVE
            }
            vector_candidates = [
                candidate
                for candidate in vector_candidates
                if candidate.source_id not in inactive_ids
            ]
            fused = RetrievalRankFusion().fuse_ranked_candidates(
                keyword_candidates,
                vector_candidates,
                limit=None,
            )
            selected_limit = min(max(0, self._limit), max(0, plan.max_items))
            selected_ids = {candidate.source_id for candidate in fused[:selected_limit]}
            trace_by_id: dict[str, RetrievalCandidateTrace] = {}
            for rank, candidate in enumerate(fused, 1):
                hits = tuple(
                    RetrievalHitTrace.model_validate(hit)
                    for hit in candidate.metadata.get("hits", [])
                )
                single_hit = hits[0] if len(hits) == 1 else None
                trace_by_id[candidate.source_id] = RetrievalCandidateTrace(
                    provider=self.name,
                    source_type="memory",
                    source_id=candidate.source_id,
                    revision_id=_optional_text(candidate.metadata.get("revision_id")),
                    native_rank=single_hit.rank if single_hit else None,
                    native_score=single_hit.score if single_hit else None,
                    fused_rank=rank,
                    fused_score=candidate.score,
                    selected_for_result=candidate.source_id in selected_ids,
                    filter_reason=(
                        None
                        if candidate.source_id in selected_ids
                        else "provider_top_k"
                    ),
                    content_preview=candidate.content[:500],
                    source_title=_optional_text(candidate.metadata.get("title")),
                    metadata={
                        key: value
                        for key, value in candidate.metadata.items()
                        if key not in {"channel_rank", "channel_score", "hits", "rrf_score", "fused_rank"}
                    },
                    hits=hits,
                    selection_stage=(
                        None
                        if candidate.source_id in selected_ids
                        else "provider"
                    ),
                )

            for rank, record in enumerate(keyword_rows, 1):
                if record.status == MemoryStatus.ACTIVE or record.id in trace_by_id:
                    continue
                vector_hit = next(
                    (
                        RetrievalHitTrace(
                            channel="vector",
                            rank=vector_rank,
                            score=_score(row.get("score")),
                        )
                        for vector_rank, row in enumerate(vector_rows, 1)
                        if str(row.get("memory_id") or "") == record.id
                    ),
                    None,
                )
                hits = [
                    RetrievalHitTrace(
                        channel="keyword",
                        rank=rank,
                        score=record.score or _keyword_score(record.content, query),
                    )
                ]
                if vector_hit is not None:
                    hits.append(vector_hit)
                trace_by_id[record.id] = RetrievalCandidateTrace(
                    provider=self.name,
                    source_type="memory",
                    source_id=record.id,
                    revision_id=_optional_text(record.metadata.get("revision_id")),
                    native_rank=rank,
                    native_score=hits[0].score,
                    selected_for_result=False,
                    filter_reason="inactive_memory",
                    content_preview=record.content[:500],
                    source_title=_optional_text(record.metadata.get("title")),
                    metadata=record.metadata,
                    hits=tuple(hits),
                    selection_stage="provider",
                )

            for rank, row in enumerate(vector_rows, 1):
                if str(row.get("memory_id") or "").strip():
                    continue
                source_id = f"missing_vector_id_{rank}"
                trace_by_id[source_id] = RetrievalCandidateTrace(
                    provider=self.name,
                    source_type="memory",
                    source_id=source_id,
                    revision_id=_optional_text(row.get("revision_id")),
                    selected_for_result=False,
                    filter_reason="missing_source_id",
                    content_preview=str(row.get("content", ""))[:500] or None,
                    hits=(
                        RetrievalHitTrace(
                            channel="vector",
                            rank=rank,
                            score=_score(row.get("score")),
                        ),
                    ),
                    selection_stage="provider",
                )

            selected = fused[:selected_limit]
            items = tuple(
                ContextItem(
                    self.name,
                    candidate.content,
                    candidate.source_id,
                    {"fused_score": candidate.score},
                )
                for candidate in selected
            )
            channels = (
                RetrievalChannelTrace(
                    channel="keyword",
                    status="succeeded",
                    hit_count=len(keyword_rows),
                ),
                RetrievalChannelTrace(
                    channel="vector",
                    status=vector_status,
                    hit_count=len(vector_rows),
                ),
            )
            return ProviderResult(
                self.name,
                "succeeded",
                items,
                candidate_count=len(trace_by_id),
                result_count=len(items),
                duration_ms=round((time.perf_counter() - started) * 1000),
                candidates=tuple(trace_by_id.values()),
                query=query,
                resource_scope={"memory_status": "active"},
                effective_params={
                    "recall_limit": limit,
                    "result_limit": selected_limit,
                },
                channels=channels,
            )
        except TimeoutError:
            return self._failed_result(
                status="timeout",
                error="memory retrieval timed out",
                query=query,
                keyword_rows=keyword_rows,
                vector_rows=vector_rows,
                keyword_status=("timeout" if processing_stage == "keyword" else keyword_status),
                vector_status=(
                    "timeout"
                    if processing_stage == "vector" and self._vector_indexer is not None and not vector_completed
                    else vector_status
                ),
                limit=limit,
                started=started,
            )
        except Exception as exc:
            return self._failed_result(
                status="failed",
                error=str(exc),
                query=query,
                keyword_rows=keyword_rows,
                vector_rows=vector_rows,
                keyword_status=("failed" if processing_stage == "keyword" else keyword_status),
                vector_status=(
                    "failed"
                    if processing_stage == "vector" and self._vector_indexer is not None and not vector_completed
                    else vector_status
                ),
                limit=limit,
                started=started,
            )

    def _vector_candidates(self, rows: list[dict[str, Any]]) -> list[RetrievalCandidate]:
        candidates: list[RetrievalCandidate] = []
        for rank, item in enumerate(rows, 1):
            memory_id = str(item.get("memory_id") or "").strip()
            if not memory_id:
                continue
            candidates.append(
                self._candidate(
                    memory_id,
                    str(item.get("content", "")),
                    _score(item.get("score")) or 0.0,
                    {
                        "channel_rank": rank,
                        "channel_score": _score(item.get("score")),
                    },
                )
            )
        return candidates

    def _failed_result(
        self,
        *,
        status: str,
        error: str,
        query: str,
        keyword_rows: list[Any],
        vector_rows: list[dict[str, Any]],
        keyword_status: str,
        vector_status: str,
        limit: int,
        started: float,
    ) -> ProviderResult:
        candidates: dict[str, RetrievalCandidateTrace] = {}
        for rank, record in enumerate(keyword_rows, 1):
            vector_hits = [
                RetrievalHitTrace(
                    channel="vector",
                    rank=vector_rank,
                    score=_score(row.get("score")),
                )
                for vector_rank, row in enumerate(vector_rows, 1)
                if str(row.get("memory_id") or "") == record.id
            ]
            hits = [
                RetrievalHitTrace(
                    channel="keyword",
                    rank=rank,
                    score=record.score or _keyword_score(record.content, query),
                ),
                *vector_hits,
            ]
            candidates[record.id] = RetrievalCandidateTrace(
                provider=self.name,
                source_type="memory",
                source_id=record.id,
                revision_id=_optional_text(record.metadata.get("revision_id")),
                native_rank=rank,
                native_score=hits[0].score,
                selected_for_result=False,
                filter_reason=(
                    "inactive_memory"
                    if record.status != MemoryStatus.ACTIVE
                    else "provider_failed_before_selection"
                ),
                content_preview=record.content[:500],
                source_title=_optional_text(record.metadata.get("title")),
                metadata=record.metadata,
                hits=tuple(hits),
                selection_stage="provider",
            )
        known_ids = set(candidates)
        for rank, row in enumerate(vector_rows, 1):
            memory_id = str(row.get("memory_id") or "").strip()
            if not memory_id:
                source_id = f"missing_vector_id_{rank}"
                candidates[source_id] = RetrievalCandidateTrace(
                    provider=self.name,
                    source_type="memory",
                    source_id=source_id,
                    selected_for_result=False,
                    filter_reason="missing_source_id",
                    content_preview=str(row.get("content", ""))[:500] or None,
                    source_title=_optional_text(row.get("title")),
                    hits=(
                        RetrievalHitTrace(
                            channel="vector",
                            rank=rank,
                            score=_score(row.get("score")),
                        ),
                    ),
                    selection_stage="provider",
                )
                continue
            if memory_id in known_ids:
                continue
            candidates[memory_id] = RetrievalCandidateTrace(
                provider=self.name,
                source_type="memory",
                source_id=memory_id,
                revision_id=_optional_text(row.get("revision_id")),
                native_rank=rank,
                native_score=_score(row.get("score")),
                selected_for_result=False,
                filter_reason="provider_failed_before_selection",
                content_preview=str(row.get("content", ""))[:500],
                source_title=_optional_text(row.get("title")),
                hits=(
                    RetrievalHitTrace(
                        channel="vector",
                        rank=rank,
                        score=_score(row.get("score")),
                    ),
                ),
                selection_stage="provider",
            )
        channels = [
            RetrievalChannelTrace(
                channel="keyword",
                status=keyword_status,
                hit_count=len(keyword_rows),
            ),
        ]
        if self._vector_indexer is not None:
            channels.append(
                RetrievalChannelTrace(
                    channel="vector",
                    status=vector_status,
                    hit_count=len(vector_rows),
                    error_message=error if vector_status in {"failed", "timeout"} else None,
                )
            )
        return ProviderResult(
            self.name,
            status,
            error_message=error,
            candidate_count=len(candidates),
            result_count=0,
            duration_ms=round((time.perf_counter() - started) * 1000),
            candidates=tuple(candidates.values()),
            query=query,
            resource_scope={"memory_status": "active"},
            effective_params={"recall_limit": limit},
            channels=tuple(channels),
        )

    @staticmethod
    def _candidate(
        source_id: str, content: str, score: float, metadata: dict[str, Any]
    ) -> RetrievalCandidate:
        return RetrievalCandidate(source_id, content, "memory", score, metadata)


def _score(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


__all__ = ["MemoryContextProvider"]
