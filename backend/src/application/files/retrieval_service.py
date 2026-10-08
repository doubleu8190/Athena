"""附件和知识库上下文检索用例。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from application.retrieval import RetrievalFusionService
from domain.files import (
    AttachmentRepository,
    AttachmentStatus,
    FileChunkRepository,
    FileVectorSearchPort,
)

from application.runs.context.contracts import ContextItem, ContextPlan, ProviderResult


def _keyword_score(content: str, query: str) -> float:
    terms = {term.lower() for term in query.split() if term.strip()}
    if not terms:
        return 0.0
    lowered = content.lower()
    return sum(term in lowered for term in terms) / len(terms)


class FileContextProvider:
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
        task: Any,
        plan: ContextPlan,
        run_id: str | None = None,
    ) -> ProviderResult:
        started = time.perf_counter()
        query = plan.file_query or getattr(task, "goal", "")
        file_ids = tuple(plan.file_ids or getattr(task, "attachment_ids", ()))
        if not query or not file_ids:
            return ProviderResult(self.name, "succeeded")
        try:
            async with asyncio.timeout(self._timeout_seconds):
                groups = await asyncio.gather(
                    *(self._chunks.list_by_attachment(file_id) for file_id in file_ids)
                )
                allowed = {chunk.id: chunk for group in groups for chunk in group}
                keyword = sorted(
                    allowed.values(),
                    key=lambda chunk: _keyword_score(chunk.content, query),
                    reverse=True,
                )[: plan.recall_limit]
                vector_rows = (
                    await self._vector_indexer.query(
                        query, plan.recall_limit, attachment_ids=set(file_ids)
                    )
                    if self._vector_indexer
                    else []
                )
            vector = [
                allowed[row.get("chunk_id")]
                for row in vector_rows
                if row.get("chunk_id") in allowed
            ]
            from domain.retrieval import RetrievalCandidate

            keyword_candidates = [
                RetrievalCandidate(
                    chunk.id,
                    chunk.content,
                    self.name,
                    _keyword_score(chunk.content, query),
                    {
                        "attachment_id": chunk.attachment_id,
                        "locator": chunk.locator.values,
                    },
                )
                for chunk in keyword
            ]
            vector_candidates = [
                RetrievalCandidate(
                    chunk.id,
                    chunk.content,
                    self.name,
                    float(
                        next(
                            (
                                row.get("score", 0.0)
                                for row in vector_rows
                                if row.get("chunk_id") == chunk.id
                            ),
                            0.0,
                        )
                    ),
                    {
                        "attachment_id": chunk.attachment_id,
                        "locator": chunk.locator.values,
                    },
                )
                for chunk in vector
            ]
            fused = RetrievalFusionService().fuse_ranked_candidates(
                keyword_candidates,
                vector_candidates,
                limit=min(
                    plan.limit_per_file * max(1, len(file_ids)), plan.max_items
                ),
            )
            items = tuple(
                ContextItem(
                    self.name,
                    item.content,
                    item.metadata.get("attachment_id"),
                    item.metadata,
                )
                for item in fused
            )
            return ProviderResult(
                self.name,
                "succeeded",
                items,
                duration_ms=round((time.perf_counter() - started) * 1000),
                candidate_count=len(keyword) + len(vector),
                result_count=len(items),
            )
        except TimeoutError:
            return ProviderResult(
                self.name,
                "timeout",
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_message="file retrieval timed out",
            )
        except Exception as exc:
            return ProviderResult(
                self.name,
                "failed",
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_message=str(exc),
            )


class KnowledgeContextProvider(FileContextProvider):
    """只检索指定知识库中处于 READY 状态的文档。"""

    name = "knowledge"

    def __init__(
        self,
        chunks: FileChunkRepository,
        attachments: AttachmentRepository,
        vector_indexer: FileVectorSearchPort | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(chunks, vector_indexer, **kwargs)
        self._attachments = attachments

    async def retrieve_context(
        self,
        *,
        session_id: str,
        task: Any,
        plan: ContextPlan,
        run_id: str | None = None,
    ) -> ProviderResult:
        knowledge_ids = tuple(
            plan.knowledge_base_ids or getattr(task, "knowledge_base_ids", ())
        )
        documents = []
        if knowledge_ids:
            for knowledge_id in knowledge_ids:
                documents.extend(
                    await self._attachments.list_by_knowledge_base(knowledge_id)
                )
        else:
            documents = await self._attachments.list_all_attachments()
        ready = [item.id for item in documents if item.status == AttachmentStatus.READY]
        knowledge_plan = plan.with_updates(
            file_ids=tuple(ready), file_query=plan.knowledge_query
        )
        result = await super().retrieve_context(
            session_id=session_id, task=task, plan=knowledge_plan, run_id=run_id
        )
        return ProviderResult(
            self.name,
            result.status,
            tuple(
                ContextItem(
                    self.name,
                    item.content,
                    item.source_id,
                    {**item.metadata, "knowledge_base_ids": knowledge_ids},
                )
                for item in result.items
            ),
            result.error_message,
            result.candidate_count,
            result.result_count,
            result.duration_ms,
        )


__all__ = ["FileContextProvider", "KnowledgeContextProvider"]
