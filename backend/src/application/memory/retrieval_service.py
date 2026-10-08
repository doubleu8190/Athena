"""记忆上下文检索用例。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from application.retrieval import RetrievalFusionService
from domain.memory import MemoryRepositoryPort, MemorySearchRequest, MemoryVectorSearchPort

from application.runs.context.contracts import ContextItem, ContextPlan, ProviderResult


def _keyword_score(content: str, query: str) -> float:
    terms = {term.lower() for term in query.split() if term.strip()}
    if not terms:
        return 0.0
    lowered = content.lower()
    return sum(term in lowered for term in terms) / len(terms)


class MemoryContextProvider:
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

    async def acquire(
        self,
        *,
        session_id: str,
        task: Any,
        plan: ContextPlan,
        run_id: str | None = None,
    ) -> ProviderResult:
        started = time.perf_counter()
        query = plan.memory_query or getattr(task, "goal", "")
        if not query:
            return ProviderResult(self.name, "succeeded")
        try:
            async with asyncio.timeout(self._timeout_seconds):
                limit = max(self._limit, plan.recall_limit)
                keyword = await self._repository.search(
                    MemorySearchRequest(
                        query=query, limit=limit, filters={"session_id": session_id}
                    )
                )
                vector = (
                    await self._vector_indexer.query(
                        query, limit, session_id=session_id
                    )
                    if self._vector_indexer
                    else []
                )
            keyword_candidates = [
                self._candidate(
                    record.id,
                    record.content,
                    record.score or _keyword_score(record.content, query),
                    record.metadata,
                )
                for record in keyword
                if not getattr(record, "status", None)
                or str(record.status) == "active"
            ]
            vector_candidates = [
                self._candidate(
                    str(item.get("memory_id")),
                    str(item.get("content", "")),
                    float(item.get("score", 0.0)),
                    {},
                )
                for item in vector
            ]
            fused = RetrievalFusionService().fuse_ranked_candidates(
                keyword_candidates,
                vector_candidates,
                limit=min(self._limit, plan.max_items),
            )
            items = tuple(
                ContextItem(
                    self.name,
                    item.content,
                    item.source_id,
                    {**item.metadata, "score": item.score},
                )
                for item in fused
            )
            return ProviderResult(
                self.name,
                "succeeded",
                items,
                duration_ms=round((time.perf_counter() - started) * 1000),
                candidate_count=len(keyword_candidates) + len(vector_candidates),
                result_count=len(items),
            )
        except TimeoutError:
            return ProviderResult(
                self.name,
                "timeout",
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_message="memory retrieval timed out",
            )
        except Exception as exc:
            return ProviderResult(
                self.name,
                "failed",
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_message=str(exc),
            )

    @staticmethod
    def _candidate(source_id: str, content: str, score: float, metadata: dict[str, Any]):
        from domain.retrieval import RetrievalCandidate

        return RetrievalCandidate(source_id, content, "memory", score, metadata)


__all__ = ["MemoryContextProvider"]
