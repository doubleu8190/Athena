"""Shared candidate processing for file and knowledge retrieval."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from athena.core.files.contracts import FileRetrievalCandidate
from athena.core.files.ports import FileReranker
from athena.core.retrieval.ports import RetrievalTraceWriter
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class RetrievalTraceRecorder:
    """Best-effort completion and candidate recording for retrieval runs."""

    def __init__(self, writer: RetrievalTraceWriter | None) -> None:
        self._writer = writer

    async def complete(
        self,
        run_id: str | None,
        started: float,
        *,
        candidate_count: int,
        selected_count: int = 0,
        status: str,
        error_message: str | None = None,
    ) -> None:
        if run_id is None or self._writer is None:
            return
        try:
            await self._writer.complete_run(
                run_id,
                candidate_count=candidate_count,
                selected_count=selected_count,
                status=status,
                duration_ms=round(
                    (asyncio.get_running_loop().time() - started) * 1000, 2
                ),
                error_message=error_message,
            )
        except Exception as exc:
            logger.warning("retrieval_trace_complete_failed", error=str(exc))

    async def record(
        self,
        run_id: str,
        candidates: Sequence[Any],
        *,
        candidate_count: int,
        selected_count: int,
        started: float,
        error_message: str | None = None,
    ) -> None:
        if self._writer is None:
            return
        try:
            await self._writer.record_candidates(run_id, candidates)
            await self.complete(
                run_id,
                started,
                candidate_count=candidate_count,
                selected_count=selected_count,
                status="partial" if error_message else "succeeded",
                error_message=error_message,
            )
        except Exception as exc:
            logger.warning("retrieval_trace_record_failed", error=str(exc))
            await self.complete(
                run_id,
                started,
                candidate_count=candidate_count,
                selected_count=selected_count,
                status="partial",
                error_message=str(exc),
            )


class HybridCandidatePipeline:
    """Apply RRF fusion, optional reranking, and result diversification."""

    def __init__(self, reranker: FileReranker | None = None) -> None:
        self.reranker = reranker

    def fuse(
        self,
        keyword: Sequence[FileRetrievalCandidate],
        vector: Sequence[FileRetrievalCandidate],
        limit: int,
        *,
        source_title: str | None = None,
        document_version: int | None = None,
    ) -> list[FileRetrievalCandidate]:
        scores: dict[str, float] = {}
        values: dict[str, FileRetrievalCandidate] = {}
        for rank, chunk in enumerate(keyword, 1):
            scores[chunk.source_id] = scores.get(chunk.source_id, 0) + 1 / (60 + rank)
            values[chunk.source_id] = FileRetrievalCandidate(
                source_id=chunk.source_id,
                content=chunk.content,
                attachment_id=chunk.attachment_id,
                locator=chunk.locator,
                source_title=chunk.source_title or source_title,
                document_version=chunk.document_version or document_version,
                keyword_score=chunk.keyword_score,
                keyword_rank=rank,
            )
        for rank, item in enumerate(vector, 1):
            scores[item.source_id] = scores.get(item.source_id, 0) + 1 / (60 + rank)
            previous = values.get(item.source_id)
            if previous is None:
                previous = values[item.source_id] = FileRetrievalCandidate(
                    source_id=item.source_id,
                    content=item.content,
                    attachment_id=item.attachment_id,
                    locator=item.locator,
                    source_title=item.source_title or source_title,
                    document_version=item.document_version or document_version,
                    metadata=dict(item.metadata),
                    vector_score=item.vector_score,
                )
            else:
                previous.vector_score = item.vector_score
            previous.vector_rank = rank
        ordered = sorted(
            values.values(), key=lambda item: scores[item.source_id], reverse=True
        )[:limit]
        for rank, item in enumerate(ordered, 1):
            item.fused_score = scores[item.source_id]
            item.fused_rank = rank
            item.source_title = item.source_title or source_title
            item.document_version = item.document_version or document_version
        return ordered

    async def rerank(
        self,
        query: str,
        candidates: list[FileRetrievalCandidate],
        limit: int,
    ) -> list[FileRetrievalCandidate]:
        if not candidates or limit <= 0:
            return []
        if self.reranker is None:
            return self._fallback(candidates, limit)
        try:
            return await self.reranker.rerank(query, candidates, limit)
        except Exception as exc:
            logger.warning("file_rerank_failed", error=str(exc))
            return self._fallback(candidates, limit)

    @staticmethod
    def _fallback(
        candidates: list[FileRetrievalCandidate], limit: int
    ) -> list[FileRetrievalCandidate]:
        for rank, candidate in enumerate(candidates[:limit], 1):
            candidate.rerank_score = candidate.fused_score
            candidate.rerank_rank = rank
        return candidates[:limit]

    @staticmethod
    def diversify(
        candidates: Sequence[FileRetrievalCandidate],
        limit: int,
        *,
        max_per_document: int,
    ) -> list[FileRetrievalCandidate]:
        selected: list[FileRetrievalCandidate] = []
        counts: dict[str, int] = {}
        for candidate in candidates:
            count = counts.get(candidate.attachment_id, 0)
            if count >= max_per_document:
                continue
            selected.append(candidate)
            counts[candidate.attachment_id] = count + 1
            if len(selected) >= limit:
                return selected
        if len(selected) < limit:
            selected_ids = {candidate.source_id for candidate in selected}
            for candidate in candidates:
                if candidate.source_id in selected_ids:
                    continue
                selected.append(candidate)
                if len(selected) >= limit:
                    break
        return selected


__all__ = ["HybridCandidatePipeline", "RetrievalTraceRecorder"]
