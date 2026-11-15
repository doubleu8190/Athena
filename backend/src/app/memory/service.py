"""记忆新增、混合召回、显式控制与原子回执用例。"""

from __future__ import annotations

import asyncio
import json
import math
import unicodedata
from builtins import list as List
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal

from app.memory.sensitive import raise_for_sensitive_texts
from app.memory.write_core import (
    MemoryEmbeddingError,
    control_arguments_digest,
    normalize_memory_write,
    normalize_text,
    normalized_persisted_text,
    prepare_write_embeddings,
    validate_write_target,
)
from common.ids import new_id
from domain.llm.ports import EmbeddingPort, TokenCounterPort
from domain.memory.lifecycle import (
    ControlCommand,
    ControlPayload,
    ControlReceiptResult,
    ControlSourceSnapshot,
    LoadPolicy,
    MemoryKind,
)
from domain.memory.model import (
    AccessContext,
    Memory,
    MemoryHit,
    MemoryRepositoryPort,
    RecallRequest,
    RecallResult,
    RerankerPort,
)
from domain.memory.ports import MemoryControlSourcePort, MemoryLifecycleRepositoryPort


class MemoryService:
    """提供记忆新增、列出、删除、设置、混合召回和显式控制。"""

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
        lifecycle: MemoryLifecycleRepositoryPort | None = None,
        control_source: MemoryControlSourcePort | None = None,
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
        self._lifecycle = lifecycle
        self._control_source = control_source

    @property
    def lifecycle_repository(self) -> MemoryLifecycleRepositoryPort | None:
        """向组合根暴露同一生命周期端口，便于 Run 终态唤醒。"""
        return self._lifecycle

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

    async def delete(self, access: AccessContext, memory_id: str, version: int) -> bool:
        if not access.namespace_id or not access.writable_scopes:
            raise ValueError("memory_scope_forbidden")
        return await self._repository.delete(access, memory_id, version)

    async def set_enabled(
        self, namespace_id: str, read_enabled: bool, auto_extract_enabled: bool
    ) -> None:
        await self._repository.update_settings(namespace_id, read_enabled, auto_extract_enabled)

    async def remember(
        self,
        access: AccessContext,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        content: str,
        scope: str,
        kind: MemoryKind,
        load_policy: LoadPolicy,
        evidence: str,
        expires_at: datetime | None = None,
    ) -> ControlReceiptResult:
        """同步保存明确授权的正文，不执行额外模型或事实组裁决。

        参数：
        - `access`：当前用户和可写 scope。
        - `run_id`、`session_id`、`tool_call_id`：固定控制来源与幂等身份。
        - `content`：用户明确要求保存的正文。
        - `scope`：personal 或 project。
        - `kind`、`load_policy`：服务端接受的记忆分类和加载策略。
        - `evidence`：当前 Run 中支持该操作的直接用户原话。
        - `expires_at`：用户明确给出的到期时间。

        返回：
        - ControlReceiptResult：与业务变更同事务提交的无正文回执。

        异常：
        - ValueError：权限、证据、意图、正文或敏感边界无效。
        """
        return await self._apply_control(
            access,
            run_id=run_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            operation="remember",
            content=content,
            scope=scope,
            kind=kind,
            load_policy=load_policy,
            evidence=evidence,
            expires_at=expires_at,
            target_memory_id=None,
            expected_version=None,
        )

    async def correct(
        self,
        access: AccessContext,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        content: str,
        scope: str,
        kind: MemoryKind,
        load_policy: LoadPolicy,
        evidence: str,
        target_memory_id: str,
        expected_version: int,
        expires_at: datetime | None = None,
    ) -> ControlReceiptResult:
        """同步纠正唯一显式目标，以新行和 superseded 链保留版本。

        参数：
        - `access`：当前用户和可写 scope。
        - `run_id`、`session_id`、`tool_call_id`：固定控制来源与幂等身份。
        - `content`：纠正后的完整正文。
        - `scope`：personal 或 project。
        - `kind`、`load_policy`：新记忆分类和加载策略。
        - `evidence`：直接用户纠正原话。
        - `target_memory_id`、`expected_version`：显式目标及并发版本。
        - `expires_at`：用户明确给出的到期时间。

        返回：
        - ControlReceiptResult：新记忆 ID、被替代旧 ID 和 generation。

        异常：
        - ValueError：目标、版本、权限、证据或内容边界无效。
        """
        if not target_memory_id.strip() or expected_version < 1:
            raise ValueError("memory_control_target_required")
        return await self._apply_control(
            access,
            run_id=run_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            operation="correct",
            content=content,
            scope=scope,
            kind=kind,
            load_policy=load_policy,
            evidence=evidence,
            expires_at=expires_at,
            target_memory_id=target_memory_id,
            expected_version=expected_version,
        )

    async def forget(
        self,
        access: AccessContext,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        scope: str,
        evidence: str,
        target_memory_id: str | None,
        expected_version: int | None,
        clear_scope: bool,
    ) -> ControlReceiptResult:
        """同步删除唯一目标或明确清空当前 scope。

        参数：
        - `access`：当前用户和可写 scope。
        - `run_id`、`session_id`、`tool_call_id`：固定控制来源与幂等身份。
        - `scope`：personal 或 project。
        - `evidence`：直接用户忘记原话。
        - `target_memory_id`、`expected_version`：单目标删除的显式身份。
        - `clear_scope`：是否清空整个授权 scope。

        返回：
        - ControlReceiptResult：与墓碑变更同事务提交的回执。

        异常：
        - ValueError：权限、意图、目标、版本或敏感边界无效。
        """
        lifecycle = self._lifecycle
        if lifecycle is None:
            raise ValueError("memory_control_unavailable")
        arguments = self._forget_control_arguments(
            access,
            run_id=run_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            scope=scope,
            evidence=evidence,
            target_memory_id=target_memory_id,
            expected_version=expected_version,
            clear_scope=clear_scope,
        )
        evidence = str(arguments["evidence"])
        raise_for_sensitive_texts((evidence,))
        arguments_digest = control_arguments_digest(arguments)
        replay = await lifecycle.control_receipt(
            run_id=run_id,
            tool_call_id=tool_call_id,
            arguments_digest=arguments_digest,
        )
        if replay is not None:
            return replay
        lifecycle, sources = await self._control_context(
            access,
            run_id=run_id,
            session_id=session_id,
            scope=scope,
            evidence=evidence,
            operation="forget",
        )
        if clear_scope:
            if target_memory_id is not None or expected_version is not None:
                raise ValueError("memory_control_arguments_invalid")
            if not self._contains_any(
                sources.messages[0].text,
                ("清空", "全部删除", "clear all", "clear memory"),
            ):
                raise ValueError("memory_control_intent_missing")
        elif target_memory_id is None or expected_version is None:
            raise ValueError("memory_control_target_required")
        scope_key = self._control_scope_key(access, sources.project_id, scope)
        state = await lifecycle.scope_state(access.namespace_id, scope_key)
        if not state.enabled:
            raise ValueError("memory_scope_disabled")
        decision_generation = await lifecycle.user_generation(access.namespace_id)
        receipt = ControlPayload(
            owner_id=access.namespace_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            operation="clear_scope" if clear_scope else "forget",
            arguments_digest=arguments_digest,
            auth_session_id=sources.auth_session_id,
            namespace_id=access.namespace_id,
            scope_key=scope_key,
        )
        return await lifecycle.apply_control(
            ControlCommand(
                receipt=receipt,
                decision_generation=decision_generation,
                access_scope_key=scope_key,
                operation="forget",
                content=None,
                kind=None,
                load_policy=None,
                evidence_sources=sources.messages,
                evidenced_at=max(item.source_at for item in sources.messages),
                expires_at=None,
                target_memory_id=target_memory_id,
                expected_version=expected_version,
                clear_scope=clear_scope,
                create_memory_id=None,
                embedding=None,
                embedding_profile_id=None,
            )
        )

    async def recover_control(
        self,
        access: AccessContext,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        operation: Literal["remember", "correct"],
        content: str,
        scope: str,
        kind: MemoryKind,
        load_policy: LoadPolicy,
        evidence: str,
        expires_at: datetime | None,
        target_memory_id: str | None,
        expected_version: int | None,
    ) -> ControlReceiptResult | None:
        """只按原参数摘要查询 remember/correct 回执，不执行新写入。

        参数：
        - `access`：当前用户授权快照。
        - `run_id`、`session_id`、`tool_call_id`：原始控制身份。
        - `operation`：remember 或 correct。
        - `content`、`scope`、`kind`、`load_policy`、`evidence`、`expires_at`：
          原始可持久化参数。
        - `target_memory_id`、`expected_version`：correct 的原始目标版本。

        返回：
        - ControlReceiptResult | None：已提交回执，或尚未找到确定结果。

        异常：
        - ValueError：控制依赖不可用。
        """
        arguments = self._control_arguments(
            access,
            run_id=run_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            operation=operation,
            content=content,
            scope=scope,
            kind=kind,
            load_policy=load_policy,
            evidence=evidence,
            expires_at=expires_at,
            target_memory_id=target_memory_id,
            expected_version=expected_version,
        )
        return await self._query_control_receipt(
            arguments,
            run_id=run_id,
            tool_call_id=tool_call_id,
        )

    async def recover_forget_control(
        self,
        access: AccessContext,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        scope: str,
        evidence: str,
        target_memory_id: str | None,
        expected_version: int | None,
        clear_scope: bool,
    ) -> ControlReceiptResult | None:
        """只按原参数摘要查询忘记控制回执，不执行新写入。

        参数：
        - `access`：当前用户授权快照。
        - `run_id`、`session_id`、`tool_call_id`：原始控制身份。
        - `scope`、`evidence`：原始范围与证据。
        - `target_memory_id`、`expected_version`：单目标删除身份。
        - `clear_scope`：是否清空整个 scope。

        返回：
        - ControlReceiptResult | None：已提交回执，或尚未找到确定结果。

        异常：
        - ValueError：控制依赖不可用。
        """
        arguments = self._forget_control_arguments(
            access,
            run_id=run_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            scope=scope,
            evidence=evidence,
            target_memory_id=target_memory_id,
            expected_version=expected_version,
            clear_scope=clear_scope,
        )
        return await self._query_control_receipt(
            arguments,
            run_id=run_id,
            tool_call_id=tool_call_id,
        )

    async def _apply_control(
        self,
        access: AccessContext,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        operation: Literal["remember", "correct"],
        content: str,
        scope: str,
        kind: MemoryKind,
        load_policy: LoadPolicy,
        evidence: str,
        expires_at: datetime | None,
        target_memory_id: str | None,
        expected_version: int | None,
    ) -> ControlReceiptResult:
        """校验显式控制并准备一次事务外向量、事务内原子提交。

        参数：
        - `access`：当前用户授权快照。
        - `run_id`、`session_id`、`tool_call_id`：固定 Run 和工具幂等身份。
        - `operation`：remember 或 correct。
        - `content`、`scope`、`kind`、`load_policy`、`evidence`、`expires_at`：
          工具显式请求。
        - `target_memory_id`、`expected_version`：correct 的并发目标。

        返回：
        - ControlReceiptResult：原子提交或重放回执。

        异常：
        - ValueError：控制参数、来源、意图、权限或敏感边界无效。
        """
        validate_write_target(
            target_memory_id=target_memory_id,
            target_version=expected_version,
            require_target=operation == "correct",
            error_code="memory_control_arguments_invalid",
        )
        lifecycle = self._lifecycle
        if lifecycle is None:
            raise ValueError("memory_control_unavailable")
        stable_arguments = self._control_arguments(
            access,
            run_id=run_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            operation=operation,
            content=content,
            scope=scope,
            kind=kind,
            load_policy=load_policy,
            evidence=evidence,
            expires_at=expires_at,
            target_memory_id=target_memory_id,
            expected_version=expected_version,
        )
        content = str(stable_arguments["content"])
        evidence = str(stable_arguments["evidence"])
        raise_for_sensitive_texts((content, evidence))
        arguments_digest = control_arguments_digest(stable_arguments)
        replay = await lifecycle.control_receipt(
            run_id=run_id,
            tool_call_id=tool_call_id,
            arguments_digest=arguments_digest,
        )
        if replay is not None:
            return replay
        lifecycle, sources = await self._control_context(
            access,
            run_id=run_id,
            session_id=session_id,
            scope=scope,
            evidence=evidence,
            operation=operation,
            content=content,
            expires_at=expires_at,
        )
        try:
            normalized_write = normalize_memory_write(
                content=content,
                evidence=evidence,
                source_text=sources.messages[0].text,
            )
        except ValueError as error:
            code = str(error)
            if code == "memory_write_content_invalid":
                raise ValueError("memory_control_content_invalid") from error
            if code == "memory_write_evidence_invalid":
                raise ValueError("memory_control_evidence_invalid") from error
            raise
        content = normalized_write.content
        evidence = normalized_write.evidence
        scope_key = self._control_scope_key(access, sources.project_id, scope)
        state = await lifecycle.scope_state(access.namespace_id, scope_key)
        if not state.enabled:
            raise ValueError("memory_scope_disabled")
        decision_generation = await lifecycle.user_generation(access.namespace_id)
        try:
            embedding = (
                await asyncio.wait_for(
                    prepare_write_embeddings(self._embedder, [content]),
                    self._timeout,
                )
            )[0]
        except MemoryEmbeddingError as error:
            raise ValueError("memory_embedding_invalid") from error
        receipt = ControlPayload(
            owner_id=access.namespace_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            operation=operation,
            arguments_digest=arguments_digest,
            auth_session_id=sources.auth_session_id,
            namespace_id=access.namespace_id,
            scope_key=scope_key,
        )
        return await lifecycle.apply_control(
            ControlCommand(
                receipt=receipt,
                decision_generation=decision_generation,
                access_scope_key=scope_key,
                operation=operation,
                content=content,
                kind=kind,
                load_policy=load_policy,
                evidence_sources=sources.messages,
                evidenced_at=max(item.source_at for item in sources.messages),
                expires_at=expires_at,
                target_memory_id=target_memory_id,
                expected_version=expected_version,
                clear_scope=False,
                create_memory_id=new_id("mem"),
                embedding=tuple(embedding),
                embedding_profile_id=self._profile,
            )
        )

    @staticmethod
    def _control_arguments(
        access: AccessContext,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        operation: Literal["remember", "correct"],
        content: str,
        scope: str,
        kind: MemoryKind,
        load_policy: LoadPolicy,
        evidence: str,
        expires_at: datetime | None,
        target_memory_id: str | None,
        expected_version: int | None,
    ) -> dict[str, Any]:
        """生成首次提交和异常恢复共用的稳定控制参数。

        参数：
        - `access`：用户授权快照。
        - `run_id`、`session_id`、`tool_call_id`：控制身份。
        - `operation`：remember 或 correct。
        - `content`、`scope`、`kind`、`load_policy`、`evidence`、`expires_at`：
          工具显式参数。
        - `target_memory_id`、`expected_version`：correct 目标。

        返回：
        - dict[str, Any]：供服务端计算 SHA-256 摘要的规范参数。

        异常：
        - ValueError：规范正文或证据为空、超长。
        """
        canonical_content = normalized_persisted_text(content)
        canonical_evidence = normalized_persisted_text(evidence)
        if not 1 <= len(canonical_content) <= 2000 or not 1 <= len(canonical_evidence) <= 2000:
            raise ValueError("memory_control_content_invalid")
        return {
            "owner_id": access.namespace_id,
            "session_id": session_id,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "operation": operation,
            "content": canonical_content,
            "requested_scope": scope,
            "kind": kind.value,
            "requested_load_policy": load_policy.value,
            "evidence": canonical_evidence,
            "expires_at": None if expires_at is None else expires_at.isoformat(),
            "requested_target_memory_id": target_memory_id,
            "requested_expected_version": expected_version,
        }

    @staticmethod
    def _forget_control_arguments(
        access: AccessContext,
        *,
        run_id: str,
        session_id: str,
        tool_call_id: str,
        scope: str,
        evidence: str,
        target_memory_id: str | None,
        expected_version: int | None,
        clear_scope: bool,
    ) -> dict[str, Any]:
        """生成首次提交和异常恢复共用的稳定忘记参数。

        参数：
        - `access`：用户授权快照。
        - `run_id`、`session_id`、`tool_call_id`：控制身份。
        - `scope`、`evidence`：范围和直接证据。
        - `target_memory_id`、`expected_version`：单目标删除身份。
        - `clear_scope`：是否清空整个 scope。

        返回：
        - dict[str, Any]：供服务端计算 SHA-256 摘要的规范参数。

        异常：
        - ValueError：规范证据为空、超长。
        """
        canonical_evidence = normalized_persisted_text(evidence)
        if not 1 <= len(canonical_evidence) <= 2000:
            raise ValueError("memory_control_content_invalid")
        return {
            "owner_id": access.namespace_id,
            "session_id": session_id,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "operation": "forget",
            "requested_scope": scope,
            "evidence": canonical_evidence,
            "target_memory_id": target_memory_id,
            "expected_version": expected_version,
            "clear_scope": clear_scope,
        }

    async def _query_control_receipt(
        self,
        arguments: dict[str, Any],
        *,
        run_id: str,
        tool_call_id: str,
    ) -> ControlReceiptResult | None:
        """按原参数摘要只读查询控制回执，不触发业务重试。

        参数：
        - `arguments`：首次调用相同的稳定参数。
        - `run_id`、`tool_call_id`：回执唯一控制身份。

        返回：
        - ControlReceiptResult | None：已提交回执或 None。

        异常：
        - ValueError：控制依赖不可用。
        """
        lifecycle = self._lifecycle
        if lifecycle is None:
            raise ValueError("memory_control_unavailable")
        arguments_digest = control_arguments_digest(arguments)
        return await lifecycle.control_receipt(
            run_id=run_id,
            tool_call_id=tool_call_id,
            arguments_digest=arguments_digest,
        )

    async def _control_context(
        self,
        access: AccessContext,
        *,
        run_id: str,
        session_id: str,
        scope: str,
        evidence: str,
        operation: str,
        content: str | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[
        MemoryLifecycleRepositoryPort,
        ControlSourceSnapshot,
    ]:
        """读取固定 Run 证据并复核实时访问范围。

        参数：
        - `access`：当前用户授权快照。
        - `run_id`、`session_id`：固定 Run 与 Session。
        - `scope`：请求范围别名。
        - `evidence`：模型给出的直接用户原话。
        - `operation`：remember、correct 或 forget。
        - `content`：需要由证据支持的正文。
        - `expires_at`：需要由证据支持的到期时间。

        返回：
        - tuple：生命周期仓储和唯一匹配来源快照。

        异常：
        - ValueError：依赖、权限、来源、意图、正文或到期证据无效。
        """
        del scope
        lifecycle = self._lifecycle
        reader = self._control_source
        if lifecycle is None or reader is None:
            raise ValueError("memory_control_unavailable")
        if not access.namespace_id or not access.writable_scopes:
            raise ValueError("memory_scope_forbidden")
        sources = await reader.read_control_source(
            owner_id=access.namespace_id,
            session_id=session_id,
            run_id=run_id,
        )
        if sources is None:
            raise ValueError("memory_source_scope_invalid")
        normalized_evidence = self._normalized_evidence(evidence)
        matching_messages = tuple(
            item
            for item in sources.messages
            if normalized_evidence
            and normalized_evidence in self._normalized_evidence(item.text)
        )
        if not normalized_evidence or len(matching_messages) != 1:
            raise ValueError("memory_control_evidence_invalid")
        matched_source = matching_messages[0]
        if operation in {"remember", "correct"}:
            # 保留 evidence 源于用户原话的校验，不再要求 content 是其连续子串。
            if content is None:
                raise ValueError("memory_control_evidence_invalid")
            if expires_at is not None and not self._datetime_supported(
                expires_at,
                self._normalized_evidence(matched_source.text),
            ):
                raise ValueError("memory_control_evidence_invalid")
        return lifecycle, replace(sources, messages=(matched_source,))

    @staticmethod
    def _datetime_supported(value: datetime, normalized_evidence: str) -> bool:
        """要求到期日期以常见中文或 ISO 形式出现在直接证据中。

        参数：
        - `value`：待保存到期时间。
        - `normalized_evidence`：归一后的用户原话。

        返回：
        - bool：证据是否包含日期表示。

        异常：
        - 无预期业务异常。
        """
        date_value = value.date()
        representations = (
            date_value.isoformat(),
            f"{date_value.year}/{date_value.month}/{date_value.day}",
            f"{date_value.year}年{date_value.month}月{date_value.day}日",
        )
        return any(item.casefold() in normalized_evidence for item in representations)

    @staticmethod
    def _normalized_evidence(value: str) -> str:
        """对原文只做 Unicode 与空白归一，用于来源子串复核。

        参数：
        - `value`：正文或证据文本。

        返回：
        - str：稳定比较文本。

        异常：
        - 无预期业务异常。
        """
        return normalize_text(value)

    @staticmethod
    def _contains_any(value: str, needles: tuple[str, ...]) -> bool:
        """检查固定控制触发词，避免模型声明替代用户原话。

        参数：
        - `value`：用户原话。
        - `needles`：固定触发词。

        返回：
        - bool：是否包含任一触发词。

        异常：
        - 无预期业务异常。
        """
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return any(needle.casefold() in normalized for needle in needles)

    @staticmethod
    def _control_scope_key(
        access: AccessContext,
        project_id: str | None,
        scope: str,
    ) -> str:
        """把范围别名绑定到当前 Run 固定项目并复核写权限。

        参数：
        - `access`：当前用户授权快照。
        - `project_id`：固定 Run 项目 ID。
        - `scope`：personal 或 project。

        返回：
        - str：可提交 scope key。

        异常：
        - ValueError：范围非法或不可写。
        """
        if scope == "personal":
            scope_key = "personal"
        elif scope == "project" and project_id is not None:
            scope_key = f"project:{project_id}"
        else:
            raise ValueError("memory_control_scope_invalid")
        if scope_key not in access.writable_scopes:
            raise ValueError("memory_scope_forbidden")
        return scope_key

    @staticmethod
    def _validate_vector(vector: List[float], error_code: str) -> List[float]:
        """检查向量是否适用于 1024 维余弦检索。

        参数：
        - `vector`：模型返回向量。
        - `error_code`：无效时稳定错误码。

        返回：
        - List[float]：原向量。

        异常：
        - ValueError：维度、有限值或零向量无效。
        """
        if len(vector) != 1024 or any(not math.isfinite(value) for value in vector):
            raise ValueError(error_code)
        if not any(vector):
            raise ValueError(error_code)
        return vector
