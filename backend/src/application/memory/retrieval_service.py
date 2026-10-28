"""记忆上下文检索用例。"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from domain.memory import (
    MemoryRepositoryPort,
    MemoryFact,
    MemoryRecord,
    MemorySearchRequest,
    MemoryStatus,
    MemoryVectorSearchPort,
    SessionMemorySettingsPort,
)
from domain.retrieval import RetrievalCandidate, RetrievalRankFusion
from domain.retrieval.trace_contracts import (
    RetrievalCandidateTrace,
    RetrievalChannelTrace,
    RetrievalHitTrace,
)
from shared.contracts.memory import memory_domain_to_schema
from shared.contracts.retrieval import RetrievalHitTraceSchema

from application.runs.context.contracts import ContextItem, ContextPlan, ProviderResult
from application.runs.context.protocols import ContextProvider
from application.memory.fact_groups import build_fact_context_group, record_fact
from application.memory.index_views import build_memory_index_item
from application.memory.query_terms import extract_memory_query_terms

logger = logging.getLogger(__name__)


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
        settings: SessionMemorySettingsPort | None = None,
    ) -> None:
        self._repository = repository
        self._vector_indexer = vector_indexer
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
        started = time.perf_counter()
        query = plan.memory_query
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
                resource_scope={
                    "memory_suppressed": True,
                    "reason": "session_memory_read_disabled",
                },
            )

        project_id = None if settings is None else settings.project_id
        limit = max(self._limit, plan.recall_limit)
        keyword_rows = []
        vector_rows: list[dict[str, Any]] = []
        keyword_status = "skipped"
        vector_status = "skipped"
        processing_stage = "keyword"
        vector_completed = False
        directory_item: ContextItem | None = None
        directory_status = "skipped"
        try:
            async with asyncio.timeout(self._timeout_seconds):
                keyword_rows = await self._repository.search(
                    MemorySearchRequest(
                        query=query,
                        limit=limit,
                        terms=extract_memory_query_terms(query),
                        filters={
                            "memory_scope": {
                                "session_id": session_id,
                                "project_id": project_id,
                            }
                        },
                    )
                )
                keyword_status = "succeeded"
                active_keyword = [
                    (rank, record)
                    for rank, record in enumerate(keyword_rows, 1)
                    if _is_active_record(record)
                    and _is_in_scope(
                        record, session_id=session_id, project_id=project_id
                    )
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

            try:
                async with asyncio.timeout(
                    _remaining_timeout(started, self._timeout_seconds)
                ):
                    directory_records = await self._repository.list_scope_index(
                        session_id=session_id,
                        project_id=project_id,
                        limit=80,
                    )
                    directory_item = await build_memory_index_item(
                        self._repository,
                        directory_records,
                        session_id=session_id,
                        project_id=project_id,
                    )
                directory_status = "succeeded" if directory_item else "skipped"
            except TimeoutError:
                directory_status = "timeout"
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Memory topic directory retrieval failed; session_id=%s",
                    session_id,
                )
                directory_status = "failed"

            inactive_ids = {
                record.id
                for record in keyword_rows
                if not _is_active_record(record)
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
            scoped_keyword_ids = {record.id for record in keyword_rows}
            record_by_id = {
                record.id: record for _, record in active_keyword
            }
            missing_records = [
                candidate.source_id
                for candidate in fused
                if candidate.source_id not in record_by_id
            ]
            if missing_records:
                record_query_limit = asyncio.Semaphore(4)

                async def fetch_record(memory_id: str) -> MemoryRecord | None:
                    async with record_query_limit:
                        return await self._repository.get(memory_id)

                async with asyncio.timeout(
                    _remaining_timeout(started, self._timeout_seconds)
                ):
                    fetched = await asyncio.gather(
                        *(fetch_record(memory_id) for memory_id in missing_records)
                    )
                record_by_id.update(
                    (record.id, record)
                    for record in fetched
                    if record is not None
                    and _is_active_record(record)
                    and _is_in_scope(
                        record, session_id=session_id, project_id=project_id
                    )
                )
            stale_candidates = [
                candidate
                for candidate in fused
                if candidate.source_id in scoped_keyword_ids
                and candidate.source_id not in record_by_id
            ]
            fused = [
                candidate
                for candidate in fused
                if candidate.source_id in record_by_id
            ]

            fact_group_cache: dict[tuple[str, str, str, str], list[MemoryRecord]] = {}
            facts_by_candidate: dict[str, MemoryFact] = {}
            fact_requests: dict[tuple[str, str, str, str], MemoryFact] = {}
            for candidate in fused:
                record = record_by_id[candidate.source_id]
                fact = record_fact(record)
                if fact is None:
                    continue
                identity = (
                    fact.scope_kind,
                    fact.scope_id,
                    fact.subject_key,
                    fact.fact_key,
                )
                facts_by_candidate[candidate.source_id] = fact
                fact_requests.setdefault(identity, fact)
            if fact_requests:
                fact_identities = tuple(fact_requests)
                group_query_limit = asyncio.Semaphore(4)

                async def fetch_fact_group(
                    identity: tuple[str, str, str, str]
                ) -> list[MemoryRecord]:
                    fact = fact_requests[identity]
                    async with group_query_limit:
                        return await self._repository.list_fact_group(fact)

                async with asyncio.timeout(
                    _remaining_timeout(started, self._timeout_seconds)
                ):
                    related_groups = await asyncio.gather(
                        *(fetch_fact_group(identity) for identity in fact_identities)
                    )
                fact_group_cache = dict(
                    zip(fact_identities, related_groups, strict=True)
                )

            groups_by_candidate = {
                memory_id: group
                for memory_id, fact in facts_by_candidate.items()
                if (
                    group := build_fact_context_group(
                        record_by_id[memory_id],
                        fact_group_cache[(
                            fact.scope_kind,
                            fact.scope_id,
                            fact.subject_key,
                            fact.fact_key,
                        )],
                    )
                ) is not None
            }
            canonical_groups: dict[str, Any] = {}
            for memory_id, group in tuple(groups_by_candidate.items()):
                groups_by_candidate[memory_id] = canonical_groups.setdefault(
                    group.group_key, group
                )

            all_items: list[ContextItem] = (
                [directory_item] if directory_item is not None else []
            )
            seen_group_keys: set[str] = set()
            group_records: dict[str, MemoryRecord] = {}
            for candidate in fused:
                record = record_by_id[candidate.source_id]
                group = groups_by_candidate.get(candidate.source_id)
                if group is None:
                    scope_metadata = {
                        key: record.metadata[key]
                        for key in ("category", "type", "session_id", "project_id")
                        if key in record.metadata
                    }
                    all_items.append(
                        ContextItem(
                            self.name,
                            candidate.content,
                            candidate.source_id,
                            {"fused_score": candidate.score, **scope_metadata},
                        )
                    )
                    continue
                for member in fact_group_cache[
                    (
                        facts_by_candidate[candidate.source_id].scope_kind,
                        facts_by_candidate[candidate.source_id].scope_id,
                        facts_by_candidate[candidate.source_id].subject_key,
                        facts_by_candidate[candidate.source_id].fact_key,
                    )
                ]:
                    group_records[member.id] = member
                if group.group_key in seen_group_keys:
                    continue
                seen_group_keys.add(group.group_key)
                all_items.append(
                    ContextItem(
                        self.name,
                        group.content,
                        group.anchor_id,
                        {
                            "fused_score": candidate.score,
                            "memory_group_key": group.group_key,
                            "memory_member_ids": list(group.member_ids),
                            "memory_conflict": group.conflicted,
                            "memory_total_count": group.total_member_count,
                            "memory_complete": group.complete,
                            "fact_cardinality": group.cardinality.value,
                            "memory_fact_identity": memory_domain_to_schema(
                                group.identity
                            ).model_dump(mode="json"),
                            "memory_group_member_value": group.member_value,
                        },
                    )
                )

            selected_limit = min(
                max(0, self._limit) + (1 if directory_item is not None else 0),
                max(0, plan.max_items),
            )
            selected_items = all_items[:selected_limit]
            selected_group_keys = {
                str(item.metadata["memory_group_key"])
                for item in selected_items
                if item.metadata.get("memory_group_key")
            }
            selected_group_members = {
                str(item.metadata["memory_group_key"]): set(
                    item.metadata.get("memory_member_ids", ())
                )
                for item in selected_items
                if item.metadata.get("memory_group_key")
            }
            selected_plain_ids = {
                item.source_id
                for item in selected_items
                if item.source_id and not item.metadata.get("memory_group_key")
            }
            trace_by_id: dict[str, RetrievalCandidateTrace] = {}
            for rank, candidate in enumerate(fused, 1):
                record = record_by_id[candidate.source_id]
                group = groups_by_candidate.get(candidate.source_id)
                metadata = {
                    **record.metadata,
                    **{
                        key: value
                        for key, value in candidate.metadata.items()
                        if key not in {"hits", "rrf_score", "fused_rank"}
                    },
                }
                if group is not None:
                    metadata.update(
                        {
                            "memory_group_key": group.group_key,
                            "memory_member_ids": list(group.member_ids),
                            "memory_conflict": group.conflicted,
                            "memory_total_count": group.total_member_count,
                            "memory_complete": group.complete,
                        }
                    )
                if group is None:
                    selected_for_result = candidate.source_id in selected_plain_ids
                    group_filter_reason = None
                else:
                    rendered_members = selected_group_members.get(
                        group.group_key, set()
                    )
                    selected_for_result = (
                        group.group_key in selected_group_keys
                        and candidate.source_id in rendered_members
                    )
                    group_filter_reason = (
                        "fact_group_limit"
                        if group.conflicted
                        and candidate.source_id not in group.member_ids
                        else (
                            "duplicate_equivalent_fact"
                            if not group.conflicted
                            and candidate.source_id not in group.member_ids
                            else None
                        )
                    )
                hits = tuple(
                    RetrievalHitTraceSchema.model_validate(hit).to_domain()
                    for hit in candidate.metadata.get("hits", [])
                )
                single_hit = hits[0] if len(hits) == 1 else None
                trace_by_id[candidate.source_id] = RetrievalCandidateTrace(
                    provider=self.name,
                    source_type="memory",
                    source_id=candidate.source_id,
                    revision_id=_optional_text(metadata.get("revision_id")),
                    native_rank=single_hit.rank if single_hit else None,
                    native_score=single_hit.score if single_hit else None,
                    fused_rank=rank,
                    fused_score=candidate.score,
                    selected_for_result=selected_for_result,
                    filter_reason=(
                        None
                        if selected_for_result
                        else group_filter_reason or "provider_top_k"
                    ),
                    content_preview=candidate.content[:500],
                    source_title=_optional_text(metadata.get("title")),
                    metadata={
                        key: value
                        for key, value in metadata.items()
                        if key not in {"channel_rank", "channel_score"}
                    },
                    hits=hits,
                    selection_stage=(
                        None
                        if selected_for_result
                        else "provider"
                    ),
                )

            for rank, candidate in enumerate(stale_candidates, 1):
                if candidate.source_id in trace_by_id:
                    continue
                hits = tuple(
                    RetrievalHitTraceSchema.model_validate(hit).to_domain()
                    for hit in candidate.metadata.get("hits", [])
                )
                trace_by_id[candidate.source_id] = RetrievalCandidateTrace(
                    provider=self.name,
                    source_type="memory",
                    source_id=candidate.source_id,
                    native_rank=hits[0].rank if len(hits) == 1 else None,
                    native_score=hits[0].score if len(hits) == 1 else None,
                    fused_rank=rank,
                    fused_score=candidate.score,
                    selected_for_result=False,
                    filter_reason="inactive_memory",
                    content_preview=candidate.content[:500],
                    metadata=candidate.metadata,
                    hits=hits,
                    selection_stage="provider",
                )

            for memory_id, record in group_records.items():
                if memory_id in trace_by_id:
                    continue
                group = next(
                    (
                        group
                        for group in groups_by_candidate.values()
                        if memory_id in group.all_member_ids
                    ),
                    None,
                )
                if group is None:
                    continue
                selected_for_result = (
                    group.group_key in selected_group_keys
                    and memory_id
                    in selected_group_members.get(group.group_key, set())
                )
                filter_reason = (
                    None
                    if selected_for_result
                    else (
                        "fact_group_limit"
                        if group.conflicted and memory_id not in group.member_ids
                        else "provider_top_k"
                    )
                )
                trace_by_id[memory_id] = RetrievalCandidateTrace(
                    provider=self.name,
                    source_type="memory",
                    source_id=memory_id,
                    revision_id=_optional_text(record.metadata.get("revision_id")),
                    selected_for_result=selected_for_result,
                    filter_reason=filter_reason,
                    content_preview=record.content[:500],
                    source_title=_optional_text(record.metadata.get("title")),
                    metadata={
                        **record.metadata,
                        "memory_group_key": group.group_key,
                        "memory_member_ids": list(group.member_ids),
                        "memory_conflict": group.conflicted,
                        "memory_total_count": group.total_member_count,
                        "memory_complete": group.complete,
                        "expanded_for_conflict": group.conflicted,
                    },
                    selection_stage=None if selected_for_result else "provider",
                )

            for rank, record in enumerate(keyword_rows, 1):
                if _is_active_record(record) or record.id in trace_by_id:
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

            if directory_item is not None:
                directory_selected = any(
                    item.provider == directory_item.provider
                    and item.source_id == directory_item.source_id
                    for item in selected_items
                )
                trace_by_id[directory_item.source_id or "memory-index"] = (
                    RetrievalCandidateTrace(
                        provider="memory_index",
                        source_type="memory_view",
                        source_id=directory_item.source_id or "memory-index",
                        native_rank=1,
                        fused_rank=1,
                        fused_score=1.0,
                        selected_for_result=directory_selected,
                        filter_reason=None if directory_selected else "provider_top_k",
                        content_preview=directory_item.content[:500],
                        source_title="记忆概览与主题目录",
                        metadata=directory_item.metadata,
                        selection_stage=None if directory_selected else "provider",
                    )
                )

            items = tuple(selected_items)
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
                RetrievalChannelTrace(
                    channel="memory_directory",
                    status=directory_status,
                    hit_count=(
                        int(directory_item.metadata.get("memory_index_source_count", 0))
                        if directory_item is not None
                        else 0
                    ),
                ),
            )
            return ProviderResult(
                self.name,
                "succeeded",
                items,
                candidate_count=len(trace_by_id),
                result_count=sum(item.provider == "memory" for item in items),
                duration_ms=round((time.perf_counter() - started) * 1000),
                candidates=tuple(trace_by_id.values()),
                query=query,
                resource_scope={
                    "memory_status": "active",
                    "fact_group_count": len(seen_group_keys),
                    "conflict_group_count": sum(
                        bool(item.metadata.get("memory_conflict")) for item in items
                    ),
                    "directory_status": directory_status,
                },
                effective_params={
                    "recall_limit": limit,
                    "result_limit": selected_limit,
                    "memory_token_budget": plan.memory_token_budget,
                    "fact_group_count": len(seen_group_keys),
                    "conflict_group_count": sum(
                        bool(item.metadata.get("memory_conflict")) for item in items
                    ),
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


def _remaining_timeout(started: float, timeout_seconds: float) -> float:
    """保持全文检索、向量检索和事实组展开共享一个总时限。"""
    return max(0.0, timeout_seconds - (time.perf_counter() - started))


def _is_active_record(record: MemoryRecord) -> bool:
    """只允许当前有效且未到期的事实进入读取上下文。"""
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


def _is_in_scope(
    record: MemoryRecord, *, session_id: str, project_id: str | None
) -> bool:
    """只允许全局、当前会话或明确绑定项目的记忆参与召回。"""
    fact = record_fact(record)
    if fact is not None:
        if fact.scope_kind == "global":
            return fact.scope_id == "global"
        if fact.scope_kind == "session":
            return fact.scope_id == session_id
        return project_id is not None and fact.scope_id == project_id
    category = record.metadata.get("category", record.metadata.get("type"))
    if category in {"preference", "profile"}:
        return True
    scope_kind = record.metadata.get("scope_kind")
    scope_id = record.metadata.get("scope_id")
    return (
        record.metadata.get("session_id") == session_id
        or (scope_kind == "global" and scope_id == "global")
        or (scope_kind == "session" and scope_id == session_id)
        or (project_id is not None and scope_kind == "project" and scope_id == project_id)
    )


__all__ = ["MemoryContextProvider"]
