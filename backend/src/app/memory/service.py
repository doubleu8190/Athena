"""记忆新增与混合召回用例。"""

from __future__ import annotations

import asyncio
import json
import math
import unicodedata
from builtins import list as List
from dataclasses import replace

from domain.memory.model import (
    AccessContext,
    AddMemory,
    EmbeddingPort,
    Memory,
    MemoryHit,
    MemoryRepositoryPort,
    RecallRequest,
    RecallResult,
    RerankerPort,
    TokenCounterPort,
)


class SimpleTokenCounter(TokenCounterPort):
    """为记忆文本提供保守的预算估算。"""

    def count(self, text: str) -> int:
        return max(1, len(text.encode("utf-8")) // 3)


class MemoryService:
    """提供记忆新增、列出、删除、设置和混合召回。"""

    def __init__(
        self,
        repository: MemoryRepositoryPort,
        embedder: EmbeddingPort,
        reranker: RerankerPort,
        counter: TokenCounterPort,
        *,
        profile_id: str = "bge-m3-1024-v1",
        vector_min_score: float = 0.70,
        timeout_seconds: float = 60.0,
        rerank_min_score: float = 0.30,
        keyword_fallback_min_score: float | None = None,
    ) -> None:
        self._repository = repository
        self._embedder = embedder
        self._reranker = reranker
        self._counter = counter
        self._profile = profile_id
        self._vector_min_score = vector_min_score
        self._timeout = timeout_seconds
        self._rerank_min_score = rerank_min_score
        self._keyword_fallback_min_score = keyword_fallback_min_score

    async def add(self, request: AddMemory) -> Memory:
        if (
            not request.access.namespace_id.strip()
            or not request.scope_key.strip()
            or request.scope_key not in request.access.writable_scopes
        ):
            raise ValueError("memory_scope_required")
        request_id = request.request_id.strip()
        if not 1 <= len(request.content.strip()) <= 2000 or not 1 <= len(request_id) <= 128:
            raise ValueError("memory_request_invalid")
        if request_id != request.request_id:
            request = replace(request, request_id=request_id)
        if (
            request.status not in {"pending", "active"}
            or request.origin
            not in {
                "manual",
                "explicit",
                "extracted",
            }
            or (request.origin == "extracted" and request.status != "pending")
        ):
            raise ValueError("memory_request_invalid")
        existing = await self._repository.get_by_request(request)
        if existing is not None:
            return existing
        if request.origin == "extracted":
            _, auto_extract_enabled = await self._repository.settings(request.access.namespace_id)
            if not auto_extract_enabled:
                raise ValueError("memory_auto_extract_disabled")
        vectors = await asyncio.wait_for(self._embedder.embed([request.content]), self._timeout)
        if len(vectors) != 1:
            raise ValueError("memory_embedding_invalid")
        embedding = self._validate_vector(vectors[0], "memory_embedding_invalid")
        return await self._repository.add(request, embedding, self._profile)

    async def recall(self, request: RecallRequest) -> RecallResult:
        try:
            return await asyncio.wait_for(self._recall(request), timeout=self._timeout)
        except TimeoutError:
            return RecallResult("unavailable", degradation_reasons=("recall_deadline",))

    async def _recall(self, request: RecallRequest) -> RecallResult:
        if not request.access.namespace_id or len(request.query.strip()) not in range(1, 2001):
            raise ValueError("memory_recall_invalid")
        if not set(request.scope_keys).issubset(request.access.readable_scopes):
            raise ValueError("memory_recall_invalid")
        if not request.scope_keys:
            return RecallResult("ok")
        if not 1 <= request.limit <= 50 or not 1 <= request.token_budget <= 8000:
            raise ValueError("memory_recall_invalid")
        try:
            enabled, _ = await self._repository.settings(request.access.namespace_id)
        except Exception:
            return RecallResult("unavailable", degradation_reasons=("settings_failed",))
        if not enabled:
            return RecallResult("disabled")
        scopes = tuple(dict.fromkeys(request.scope_keys))
        keyword_task = asyncio.create_task(
            self._repository.search_keyword(request.access.namespace_id, scopes, request.query, 40)
        )
        vector_task = asyncio.create_task(self._vector_candidates(request, scopes))
        try:
            results = await asyncio.wait_for(
                asyncio.gather(keyword_task, vector_task, return_exceptions=True), self._timeout
            )
        except TimeoutError:
            for task in (keyword_task, vector_task):
                task.cancel()
            await asyncio.gather(keyword_task, vector_task, return_exceptions=True)
            return RecallResult("unavailable", degradation_reasons=("recall_deadline",))
        keyword_rows, vector_rows = results
        failures: list[str] = []
        candidates: dict[str, tuple[Memory, dict[str, tuple[int, float]]]] = {}
        for channel, rows in (("keyword", keyword_rows), ("vector", vector_rows)):
            if isinstance(rows, BaseException):
                failures.append(f"{channel}_failed")
                continue
            for rank, (memory, score) in enumerate(rows, 1):
                if channel == "vector" and score < self._vector_min_score:
                    continue
                current_candidate = candidates.get(memory.id)
                if current_candidate is None:
                    current_candidate = (memory, {})
                    candidates[memory.id] = current_candidate
                if current_candidate[0].version == memory.version:
                    current_candidate[1].setdefault(channel, (rank, score))
        if not candidates:
            try:
                snapshot = await self._repository.get_many((), request.access.namespace_id, scopes)
            except Exception:
                return RecallResult("unavailable", degradation_reasons=("final_check_failed",))
            if not snapshot.read_enabled:
                return RecallResult("disabled")
            return RecallResult(
                "unavailable" if len(failures) == 2 else ("degraded" if failures else "ok"),
                degradation_reasons=tuple(failures),
            )
        fused = []
        priority = {scope: index for index, scope in enumerate(request.scope_priority)}
        for memory, channel_scores in candidates.values():
            score = sum(
                weight / (60 + rank)
                for channel, (rank, _) in channel_scores.items()
                for weight in [0.25 if channel == "keyword" else 0.75]
            )
            fused.append((memory, score, tuple(channel_scores)))
        fused.sort(
            key=lambda item: (
                -item[1],
                priority.get(item[0].scope_key, 999),
                -item[0].updated_at.timestamp(),
                item[0].id,
            )
        )
        fused = fused[:50]
        relevance: dict[str, float] = {}
        try:
            scores = await asyncio.wait_for(
                self._reranker.score(request.query, [item[0].content for item in fused]),
                self._timeout,
            )
            if len(scores) != len(fused) or any(not math.isfinite(score) for score in scores):
                raise ValueError("memory_reranker_invalid")
            relevance = {item[0].id: score for item, score in zip(fused, scores, strict=True)}
            fused = [item for item in fused if relevance[item[0].id] >= self._rerank_min_score]
            fused.sort(key=lambda item: (-relevance[item[0].id], -item[1], item[0].id))
        except Exception:
            failures.append("reranker_failed")
            relevance = {}
            fused = [
                item
                for item in fused
                if any(
                    score >= self._vector_min_score
                    if channel == "vector"
                    else self._keyword_fallback_min_score is not None
                    and score >= self._keyword_fallback_min_score
                    for channel, (_, score) in candidates[item[0].id][1].items()
                )
            ]
        try:
            snapshot = await self._repository.get_many(
                tuple(item[0].id for item in fused), request.access.namespace_id, scopes
            )
        except Exception:
            return RecallResult("unavailable", degradation_reasons=("final_check_failed",))
        if not snapshot.read_enabled:
            return RecallResult("disabled")
        current_rows = snapshot.memories
        grouped_hits: list[MemoryHit] = []
        group_index: dict[tuple[str, str, str], int] = {}
        for memory, score, channels in fused:
            current_memory = current_rows.get(memory.id)
            if current_memory is None or current_memory.version != memory.version:
                continue
            group_key = (
                current_memory.namespace_id,
                current_memory.scope_key,
                " ".join(unicodedata.normalize("NFKC", current_memory.content).split()).casefold(),
            )
            hit = MemoryHit(
                current_memory,
                relevance.get(current_memory.id, score),
                channels,
            )
            existing_index = group_index.get(group_key)
            if existing_index is None:
                group_index[group_key] = len(grouped_hits)
                grouped_hits.append(hit)
                continue

            existing = grouped_hits[existing_index]
            source_by_key = {
                (source.kind, source.reference): source
                for source in (*existing.memory.sources, *current_memory.sources)
            }
            newest = min(
                (existing.memory, current_memory),
                key=lambda item: (-item.updated_at.timestamp(), item.id),
            )
            grouped_memory = replace(
                newest,
                sources=tuple(source_by_key.values()),
            )
            grouped_hits[existing_index] = replace(
                existing,
                memory=grouped_memory,
                channels=tuple(dict.fromkeys((*existing.channels, *channels))),
            )

        hits: list[MemoryHit] = []
        used = 0
        truncated = False
        for hit in grouped_hits:
            current_memory = hit.memory
            serialized = json.dumps(
                {
                    "scope": current_memory.scope_key,
                    "content": current_memory.content,
                    "sources": [
                        (source.kind, source.reference) for source in current_memory.sources
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            cost = self._counter.count(serialized)
            if len(hits) >= request.limit or used + cost > request.token_budget:
                truncated = True
                continue
            hits.append(MemoryHit(current_memory, hit.score, hit.channels))
            used += cost
        status = "degraded" if failures else "ok"
        return RecallResult(status, tuple(hits), used, truncated, tuple(failures))

    async def _vector_candidates(
        self, request: RecallRequest, scopes: tuple[str, ...]
    ) -> list[tuple[Memory, float]]:
        vector = await asyncio.wait_for(self._embedder.embed([request.query]), self._timeout)
        if len(vector) != 1:
            raise ValueError("memory_query_embedding_invalid")
        query_vector = self._validate_vector(vector[0], "memory_query_embedding_invalid")
        return await self._repository.search_vector(
            request.access.namespace_id, scopes, query_vector, self._profile, 40
        )

    async def list(
        self, access: AccessContext, scope_key: str, limit: int = 50, offset: int = 0
    ) -> tuple[Memory, ...]:
        if scope_key not in access.readable_scopes:
            raise ValueError("memory_scope_forbidden")
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("memory_pagination_invalid")
        return await self._repository.list(access, scope_key, limit, offset)

    async def edit(
        self, access: AccessContext, memory_id: str, content: str, expected_version: int
    ) -> bool:
        if not access.namespace_id or not access.writable_scopes:
            raise ValueError("memory_scope_forbidden")
        if not 1 <= len(content.strip()) <= 2000:
            raise ValueError("memory_content_invalid")
        vectors = await asyncio.wait_for(self._embedder.embed([content]), self._timeout)
        if len(vectors) != 1:
            raise ValueError("memory_embedding_invalid")
        embedding = self._validate_vector(vectors[0], "memory_embedding_invalid")
        return await self._repository.edit(
            access, memory_id, content, embedding, self._profile, expected_version
        )

    async def confirm(self, access: AccessContext, memory_id: str, expected_version: int) -> bool:
        if not access.namespace_id or not access.writable_scopes:
            raise ValueError("memory_scope_forbidden")
        return await self._repository.confirm(access, memory_id, expected_version)

    async def delete(self, access: AccessContext, memory_id: str, version: int) -> bool:
        if not access.namespace_id or not access.writable_scopes:
            raise ValueError("memory_scope_forbidden")
        return await self._repository.delete(access, memory_id, version)

    async def set_enabled(
        self, namespace_id: str, read_enabled: bool, auto_extract_enabled: bool
    ) -> None:
        await self._repository.update_settings(namespace_id, read_enabled, auto_extract_enabled)

    @staticmethod
    def _validate_vector(vector: List[float], error_code: str) -> List[float]:
        """检查模型输出是否适用于1024维余弦检索。"""
        if len(vector) != 1024 or any(not math.isfinite(value) for value in vector):
            raise ValueError(error_code)
        if not any(vector):
            raise ValueError(error_code)
        return vector
