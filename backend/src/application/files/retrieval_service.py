"""附件和知识库上下文检索用例。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from domain.files import (
    AttachmentRepository,
    AttachmentStatus,
    FileChunkRepository,
    FileVectorSearchPort,
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


class FileContextProvider(ContextProvider):
    """在指定附件范围内执行文件关键词与向量召回。"""

    name = "file"

    def __init__(
        self,
        chunks: FileChunkRepository,
        vector_indexer: FileVectorSearchPort | None = None,
        *,
        limit: int = 5,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._chunks = chunks
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
        query = plan.file_query
        file_ids = plan.file_ids
        if not file_ids:
            return ProviderResult(
                self.name,
                "skipped",
                error_message="no_valid_attachments",
                query=query,
                resource_scope={"attachment_ids": []},
            )

        keyword_chunks = []
        vector_rows: list[dict[str, Any]] = []
        allowed: dict[str, Any] = {}
        keyword_status = "skipped"
        vector_status = "skipped"
        processing_stage = "keyword"
        vector_completed = False
        try:
            async with asyncio.timeout(self._timeout_seconds):
                groups = await asyncio.gather(
                    *(self._chunks.list_by_attachment(file_id) for file_id in file_ids)
                )
                allowed = {chunk.id: chunk for group in groups for chunk in group}
                keyword_chunks = sorted(
                    allowed.values(),
                    key=lambda chunk: _keyword_score(chunk.content, query),
                    reverse=True,
                )[: max(0, plan.recall_limit)]
                keyword_status = "succeeded"
                keyword_candidates = [
                    self._candidate(
                        chunk.id,
                        chunk.content,
                        _keyword_score(chunk.content, query),
                        chunk.attachment_id,
                        chunk.locator.values,
                        rank,
                        chunk.metadata.values,
                    )
                    for rank, chunk in enumerate(keyword_chunks, 1)
                ]
                processing_stage = "vector"
                if self._vector_indexer is not None:
                    vector_rows = await self._vector_indexer.query(
                        query,
                        plan.recall_limit,
                        attachment_ids=set(file_ids),
                    )
                    vector_status = "succeeded"
                    vector_completed = True

            vector_chunks: list[tuple[int, Any, float]] = []
            seen_vector: set[str] = set()
            for rank, row in enumerate(vector_rows, 1):
                chunk_id = str(row.get("chunk_id") or "")
                chunk = allowed.get(chunk_id)
                if chunk is None or chunk_id in seen_vector:
                    continue
                seen_vector.add(chunk_id)
                vector_chunks.append((rank, chunk, _score(row.get("score"))))
            vector_candidates = [
                self._candidate(
                    chunk.id,
                    chunk.content,
                    score,
                    chunk.attachment_id,
                    chunk.locator.values,
                    rank,
                    chunk.metadata.values,
                )
                for rank, chunk, score in vector_chunks
            ]
            fused = RetrievalRankFusion().fuse_ranked_candidates(
                keyword_candidates,
                vector_candidates,
                limit=None,
            )
            result_limit = min(
                max(0, plan.limit_per_file * max(1, len(file_ids))),
                max(0, plan.max_items),
            )
            traces: list[RetrievalCandidateTrace] = []
            items: list[ContextItem] = []
            for rank, candidate in enumerate(fused, 1):
                selected = rank <= result_limit
                hits = tuple(
                    RetrievalHitTrace.model_validate(hit)
                    for hit in candidate.metadata.get("hits", [])
                )
                stable_metadata = {
                    key: value
                    for key, value in candidate.metadata.items()
                    if key not in {"channel_rank", "channel_score", "hits", "rrf_score", "fused_rank"}
                }
                single_hit = hits[0] if len(hits) == 1 else None
                source_metadata = stable_metadata.get("source_metadata", {})
                if not isinstance(source_metadata, dict):
                    source_metadata = {}
                traces.append(
                    RetrievalCandidateTrace(
                        provider=self.name,
                        source_type="file_chunk",
                        source_id=candidate.source_id,
                        logical_source_id=str(stable_metadata.get("attachment_id") or "") or None,
                        revision_id=_optional_text(source_metadata.get("revision_id")),
                        native_rank=single_hit.rank if single_hit else None,
                        native_score=single_hit.score if single_hit else None,
                        fused_rank=rank,
                        fused_score=candidate.score,
                        selected_for_result=selected,
                        filter_reason=None if selected else "provider_top_k",
                        content_preview=candidate.content[:500],
                        source_title=_optional_text(source_metadata.get("title")),
                        locator=dict(stable_metadata.get("locator") or {}),
                        metadata=stable_metadata,
                        hits=hits,
                        selection_stage=None if selected else "provider",
                    )
                )
                if selected:
                    items.append(
                        ContextItem(
                            self.name,
                            candidate.content,
                            candidate.source_id,
                            stable_metadata,
                        )
                    )

            channels = (
                RetrievalChannelTrace(
                    channel="keyword",
                    status="succeeded",
                    hit_count=len(keyword_chunks),
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
                tuple(items),
                candidate_count=len(traces),
                result_count=len(items),
                duration_ms=round((time.perf_counter() - started) * 1000),
                candidates=tuple(traces),
                query=query,
                resource_scope={"attachment_ids": list(file_ids)},
                effective_params={
                    "recall_limit": plan.recall_limit,
                    "result_limit": result_limit,
                },
                channels=channels,
            )
        except TimeoutError:
            return _file_failure(
                provider=self.name,
                status="timeout",
                error="file retrieval timed out",
                query=query,
                file_ids=file_ids,
                keyword_chunks=keyword_chunks,
                vector_rows=vector_rows,
                allowed=allowed,
                keyword_status=("timeout" if processing_stage == "keyword" else keyword_status),
                vector_status=(
                    "timeout"
                    if processing_stage == "vector" and self._vector_indexer is not None and not vector_completed
                    else vector_status
                ),
                recall_limit=plan.recall_limit,
                started=started,
            )
        except Exception as exc:
            return _file_failure(
                provider=self.name,
                status="failed",
                error=str(exc),
                query=query,
                file_ids=file_ids,
                keyword_chunks=keyword_chunks,
                vector_rows=vector_rows,
                allowed=allowed,
                keyword_status=("failed" if processing_stage == "keyword" else keyword_status),
                vector_status=(
                    "failed"
                    if processing_stage == "vector" and self._vector_indexer is not None and not vector_completed
                    else vector_status
                ),
                recall_limit=plan.recall_limit,
                started=started,
            )

    @staticmethod
    def _candidate(
        source_id: str,
        content: str,
        score: float | None,
        attachment_id: str,
        locator: dict[str, Any],
        rank: int,
        source_metadata: dict[str, Any],
    ) -> RetrievalCandidate:
        return RetrievalCandidate(
            source_id,
            content,
            "file",
            score or 0.0,
            {
                "attachment_id": attachment_id,
                "locator": locator,
                "source_metadata": source_metadata,
                "channel_rank": rank,
                "channel_score": score,
            },
        )


class KnowledgeContextProvider(FileContextProvider):
    """只检索指定知识库中处于 READY 状态的文档。"""

    name = "knowledge"

    def __init__(
        self,
        chunks: FileChunkRepository,
        attachments: AttachmentRepository,
        vector_indexer: FileVectorSearchPort | None = None,
        *,
        limit: int = 5,
        timeout_seconds: float = 5.0,
    ) -> None:
        super().__init__(
            chunks,
            vector_indexer,
            limit=limit,
            timeout_seconds=timeout_seconds,
        )
        self._attachments = attachments

    async def retrieve_context(
        self,
        *,
        session_id: str,
        run_id: str,
        plan: ContextPlan,
    ) -> ProviderResult:
        knowledge_ids = plan.knowledge_base_ids
        if not knowledge_ids:
            return ProviderResult(
                self.name,
                "skipped",
                error_message="no_knowledge_bases_available",
                query=plan.knowledge_query,
                resource_scope={"knowledge_base_ids": []},
            )
        documents = []
        for knowledge_id in knowledge_ids:
            documents.extend(
                await self._attachments.list_by_knowledge_base(knowledge_id)
            )
        ready_ids = tuple(
            item.id for item in documents if item.status == AttachmentStatus.READY
        )
        knowledge_plan = plan.with_updates(
            file_ids=ready_ids, file_query=plan.knowledge_query
        )
        result = await super().retrieve_context(
            session_id=session_id,
            run_id=run_id,
            plan=knowledge_plan,
        )
        items = tuple(
            ContextItem(
                self.name,
                item.content,
                item.source_id,
                {**item.metadata, "knowledge_base_ids": list(knowledge_ids)},
            )
            for item in result.items
        )
        candidates = tuple(
            candidate.model_copy(
                update={
                    "provider": self.name,
                    "source_type": "knowledge_chunk",
                    "metadata": {
                        **candidate.metadata,
                        "knowledge_base_ids": list(knowledge_ids),
                    },
                }
            )
            for candidate in result.candidates
        )
        return ProviderResult(
            self.name,
            result.status,
            items,
            result.error_message,
            result.candidate_count,
            result.result_count,
            result.duration_ms,
            candidates,
            plan.knowledge_query,
            {"knowledge_base_ids": list(knowledge_ids), "ready_attachment_ids": list(ready_ids)},
            result.effective_params,
            result.channels,
        )


def _file_failure(
    *,
    provider: str,
    status: str,
    error: str,
    query: str,
    file_ids: tuple[str, ...],
    keyword_chunks: list[Any],
    vector_rows: list[dict[str, Any]],
    allowed: dict[str, Any],
    keyword_status: str,
    vector_status: str,
    recall_limit: int,
    started: float,
) -> ProviderResult:
    candidates: dict[str, RetrievalCandidateTrace] = {}
    vector_hits: dict[str, RetrievalHitTrace] = {}
    for rank, row in enumerate(vector_rows, 1):
        chunk_id = str(row.get("chunk_id") or "")
        if chunk_id and chunk_id in allowed:
            vector_hits.setdefault(
                chunk_id,
                RetrievalHitTrace(
                    channel="vector",
                    rank=rank,
                    score=_score(row.get("score")),
                ),
            )
    for rank, chunk in enumerate(keyword_chunks, 1):
        hits = [
            RetrievalHitTrace(
                channel="keyword",
                rank=rank,
                score=_keyword_score(chunk.content, query),
            )
        ]
        if chunk.id in vector_hits:
            hits.append(vector_hits[chunk.id])
        candidates[chunk.id] = RetrievalCandidateTrace(
            provider=provider,
            source_type="file_chunk" if provider == "file" else "knowledge_chunk",
            source_id=chunk.id,
            logical_source_id=chunk.attachment_id,
            revision_id=_optional_text(chunk.metadata.values.get("revision_id")),
            native_rank=rank,
            native_score=hits[0].score,
            selected_for_result=False,
            filter_reason="provider_failed_before_selection",
            content_preview=chunk.content[:500],
            source_title=_optional_text(chunk.metadata.values.get("title")),
            locator=dict(chunk.locator.values),
            metadata={
                "attachment_id": chunk.attachment_id,
                "locator": dict(chunk.locator.values),
                "source_metadata": dict(chunk.metadata.values),
            },
            hits=tuple(hits),
            selection_stage="provider",
        )
    known_ids = set(candidates)
    for rank, row in enumerate(vector_rows, 1):
        chunk_id = str(row.get("chunk_id") or "")
        chunk = allowed.get(chunk_id)
        if chunk is None or chunk_id in known_ids:
            continue
        candidates[chunk_id] = RetrievalCandidateTrace(
            provider=provider,
            source_type="file_chunk" if provider == "file" else "knowledge_chunk",
            source_id=chunk.id,
            logical_source_id=chunk.attachment_id,
            revision_id=_optional_text(chunk.metadata.values.get("revision_id")),
            native_rank=rank,
            native_score=_score(row.get("score")),
            selected_for_result=False,
            filter_reason="provider_failed_before_selection",
            content_preview=chunk.content[:500],
            source_title=_optional_text(chunk.metadata.values.get("title")),
            locator=dict(chunk.locator.values),
            metadata={
                "attachment_id": chunk.attachment_id,
                "locator": dict(chunk.locator.values),
                "source_metadata": dict(chunk.metadata.values),
            },
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
            hit_count=len(keyword_chunks),
        )
    ]
    channels.append(
        RetrievalChannelTrace(
            channel="vector",
            status=vector_status,
            hit_count=len(vector_rows),
            error_message=error if vector_status in {"failed", "timeout"} else None,
        )
    )
    return ProviderResult(
        provider,
        status,
        error_message=error,
        candidate_count=len(candidates),
        duration_ms=round((time.perf_counter() - started) * 1000),
        candidates=tuple(candidates.values()),
        query=query,
        resource_scope={"attachment_ids": list(file_ids)},
        effective_params={"recall_limit": recall_limit},
        channels=tuple(channels),
    )


def _score(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


__all__ = ["FileContextProvider", "KnowledgeContextProvider"]
