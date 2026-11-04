"""附件和知识库上下文检索用例。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any

from domain.files import (
    AttachmentRepository,
    AttachmentStatus,
    FileChunkRepository,
    FileVectorSearchPort,
)
from domain.retrieval import (
    RetrievalCandidate,
    RetrievalRankFusion,
    RetrievalRerankPort,
)
from domain.retrieval.trace_contracts import (
    RetrievalCandidateTrace,
    RetrievalChannelTrace,
    RetrievalHitTrace,
)
from application.contracts.retrieval import RetrievalHitTraceSchema

from application.runs.context.contracts import ContextItem, ContextPlan, ProviderResult
from application.runs.context.protocols import ContextProvider
from application.runs.context.status import aggregate_query_status

logger = logging.getLogger(__name__)


def _keyword_score(content: str, query: str) -> float:
    """计算查询词在候选文本中的匹配比例。

    参数：
    - `content`（str）：待处理或返回的正文内容；约束：无额外运行时限制。
    - `query`（str）：用于检索、匹配或解析的原始查询文本；约束：无额外运行时限制。

    返回：
    - float：查询词在文本中的命中比例；查询词集合为空时返回 0.0。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
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
        reranker: RetrievalRerankPort | None = None,
        candidate_limit: int | None = None,
        rerank_result_limit: int | None = None,
        rerank_min_score: float | None = None,
        limit: int = 5,
        timeout_seconds: float = 5.0,
    ) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `chunks`（FileChunkRepository）：按顺序读取的文件字节块异步迭代器；约束：无额外运行时限制。
        - `vector_indexer`（FileVectorSearchPort | None）：执行向量检索或索引操作的可选依赖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `reranker`（RetrievalRerankPort | None）：对融合候选执行重排的可选能力；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `candidate_limit`（int | None）：单通道进入融合的候选上限；None 表示沿用计划上限；约束：可省略，默认值为 `None`。
        - `rerank_result_limit`（int | None）：重排后最多保留的候选数；None 表示沿用计划预算；约束：可省略，默认值为 `None`。
        - `rerank_min_score`（float | None）：无正关键词命中的候选须达到的重排分数门槛；None 表示不启用准入门槛。
        - `limit`（int）：本次读取或处理的数量上限；约束：可省略，默认值为 `5`。
        - `timeout_seconds`（float）：本次操作允许使用的最长秒数；约束：可省略，默认值为 `5.0`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._chunks = chunks
        self._vector_indexer = vector_indexer
        self._reranker = reranker
        self._candidate_limit = candidate_limit
        self._rerank_result_limit = rerank_result_limit
        self._rerank_min_score = rerank_min_score
        self._limit = limit
        self._timeout_seconds = timeout_seconds

    async def retrieve_context(
        self,
        *,
        session_id: str,
        run_id: str,
        plan: ContextPlan,
    ) -> ProviderResult:
        """按计划检索文件或知识库上下文并生成候选项和通道追踪。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
        - `plan`（ContextPlan）：当前计划对象，提供请求的检索或执行安排；约束：无额外运行时限制。

        返回：
        - ProviderResult：检索提供方的状态、候选结果和追踪信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
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
        keyword_candidates: list[RetrievalCandidate] = []
        vector_candidates: list[RetrievalCandidate] = []
        vector_rows: list[dict[str, Any]] = []
        allowed: dict[str, Any] = {}
        keyword_status = "skipped"
        vector_status = "skipped"
        rerank_status = "skipped"
        keyword_error: str | None = None
        vector_error: str | None = None
        rerank_error: str | None = None
        rerank_hit_count = 0
        channel_limit = min(
            max(0, plan.recall_limit),
            max(0, self._candidate_limit)
            if self._candidate_limit is not None
            else max(0, plan.recall_limit),
        )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                # 只装载本次计划授权的附件文本，向量结果随后也必须命中该集合中的文本块 ID。
                groups = await asyncio.gather(
                    *(self._chunks.list_by_attachment(file_id) for file_id in file_ids)
                )
                allowed = {chunk.id: chunk for group in groups for chunk in group}
        except TimeoutError:
            return _file_failure(
                provider=self.name,
                status="timeout",
                error="file scope loading timed out",
                query=query,
                file_ids=file_ids,
                keyword_chunks=keyword_chunks,
                vector_rows=vector_rows,
                allowed=allowed,
                keyword_status="timeout",
                vector_status="timeout",
                keyword_error="file scope loading timed out",
                vector_error="file scope loading timed out",
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
                keyword_status="failed",
                vector_status="failed",
                keyword_error=str(exc),
                vector_error=str(exc),
                recall_limit=plan.recall_limit,
                started=started,
            )

        try:
            scored_chunks = [
                (_keyword_score(chunk.content, query), chunk)
                for chunk in allowed.values()
            ]
            positive_chunks = sorted(
                (
                    (score, chunk)
                    for score, chunk in scored_chunks
                    if score > 0
                ),
                key=lambda item: (-item[0], item[1].id),
            )[:channel_limit]
            keyword_chunks = [chunk for _, chunk in positive_chunks]
            keyword_candidates = [
                self._candidate(
                    chunk.id,
                    chunk.content,
                    score,
                    chunk.attachment_id,
                    chunk.locator.values,
                    rank,
                    chunk.metadata.values,
                )
                for rank, (score, chunk) in enumerate(positive_chunks, 1)
            ]
            keyword_status = "succeeded"
        except Exception as exc:
            keyword_status = "failed"
            keyword_error = str(exc)
            logger.exception("File keyword retrieval failed")

        if self._vector_indexer is not None:
            try:
                async with asyncio.timeout(
                    max(
                        0.001,
                        self._timeout_seconds
                        - (time.perf_counter() - started),
                    )
                ):
                    vector_rows = await self._vector_indexer.query(
                        query,
                        channel_limit,
                        attachment_ids=set(file_ids),
                    )
                # 丢弃越权或重复的向量命中，保持两条通道的来源一致。
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
                vector_status = "succeeded"
            except TimeoutError:
                vector_status = "timeout"
                vector_error = "file vector retrieval timed out"
            except Exception as exc:
                vector_status = "failed"
                vector_error = str(exc)
                logger.exception("File vector retrieval failed")

        retrieval_status = aggregate_query_status(keyword_status, vector_status)
        if retrieval_status not in {"succeeded", "partial"}:
            return _file_failure(
                provider=self.name,
                status=retrieval_status,
                error=vector_error or keyword_error or "file retrieval failed",
                query=query,
                file_ids=file_ids,
                keyword_chunks=keyword_chunks,
                vector_rows=vector_rows,
                allowed=allowed,
                keyword_status=keyword_status,
                vector_status=vector_status,
                keyword_error=keyword_error,
                vector_error=vector_error,
                recall_limit=plan.recall_limit,
                started=started,
            )

        fused_candidates = RetrievalRankFusion().fuse_ranked_candidates(
            keyword_candidates,
            vector_candidates,
            limit=None,
        )
        rerank_input_limit = min(
            len(fused_candidates),
            max(0, plan.rerank_limit),
            channel_limit,
        )
        rerank_output_limit = min(
            rerank_input_limit,
            max(0, self._rerank_result_limit)
            if self._rerank_result_limit is not None
            else rerank_input_limit,
        )
        rerank_input = fused_candidates[:rerank_input_limit]
        ranked_candidates = list(rerank_input)
        rerank_succeeded = False
        if self._reranker is not None and rerank_input_limit > 0:
            try:
                async with asyncio.timeout(
                    max(
                        0.001,
                        self._timeout_seconds
                        - (time.perf_counter() - started),
                    )
                ):
                    ranked = await self._reranker.rerank(
                        query,
                        rerank_input,
                        limit=rerank_input_limit,
                    )
                if not ranked:
                    raise ValueError("reranker returned no candidates")
                allowed_candidates = {
                    candidate.source_id: candidate
                    for candidate in rerank_input
                }
                reranked: list[RetrievalCandidate] = []
                seen_reranked: set[str] = set()
                for result in ranked:
                    source_id = result.candidate.source_id
                    candidate = allowed_candidates.get(source_id)
                    if candidate is None or source_id in seen_reranked:
                        continue
                    seen_reranked.add(source_id)
                    reranked.append(
                        replace(
                            candidate,
                            metadata={
                                **candidate.metadata,
                                "rerank_rank": result.rank,
                                "rerank_score": result.score,
                            },
                        )
                    )
                if not reranked:
                    raise ValueError("reranker returned no known candidates")
                ranked_ids = {candidate.source_id for candidate in reranked}
                ranked_candidates = [
                    *reranked,
                    *(
                        candidate
                        for candidate in rerank_input
                        if candidate.source_id not in ranked_ids
                    ),
                ]
                rerank_status = "succeeded"
                rerank_hit_count = len(reranked)
                rerank_succeeded = True
            except TimeoutError:
                rerank_status = "timeout"
                rerank_error = "file rerank timed out"
            except Exception as exc:
                rerank_status = "failed"
                rerank_error = str(exc)
                logger.exception("File rerank failed")

        rejected_ids: set[str] = set()
        if rerank_succeeded and self._rerank_min_score is not None:
            admitted: list[RetrievalCandidate] = []
            for candidate in ranked_candidates:
                rerank_score = _score(candidate.metadata.get("rerank_score"))
                if _has_positive_keyword_hit(candidate) or (
                    rerank_score is not None
                    and rerank_score >= self._rerank_min_score
                ):
                    admitted.append(candidate)
                else:
                    rejected_ids.add(candidate.source_id)
            eligible_candidates = admitted[:rerank_output_limit]
        else:
            eligible_candidates = ranked_candidates[:rerank_output_limit]

        result_limit = min(
            max(0, plan.limit_per_file * max(1, len(file_ids))),
            max(0, plan.max_items),
            (
                max(0, self._rerank_result_limit)
                if self._rerank_result_limit is not None
                else max(0, plan.max_items)
            ),
        )
        selected_candidates = eligible_candidates[:result_limit]
        selected_ids = {
            candidate.source_id for candidate in selected_candidates
        }
        rerank_limit_ids = {
            candidate.source_id
            for candidate in fused_candidates[rerank_input_limit:]
        }
        trace_candidates = [
            *ranked_candidates,
            *fused_candidates[rerank_input_limit:],
        ]
        status = aggregate_query_status(
            keyword_status,
            vector_status,
            rerank_status,
        )
        traces: list[RetrievalCandidateTrace] = []
        items: list[ContextItem] = []
        for rank, candidate in enumerate(trace_candidates, 1):
            selected = candidate.source_id in selected_ids
            filter_reason = (
                None
                if selected
                else (
                    "below_rerank_min_score"
                    if candidate.source_id in rejected_ids
                    else (
                        "rerank_candidate_limit"
                        if candidate.source_id in rerank_limit_ids
                        else "provider_top_k"
                    )
                )
            )
            hits = tuple(
                RetrievalHitTraceSchema.model_validate(hit).to_domain()
                for hit in candidate.metadata.get("hits", [])
            )
            stable_metadata = {
                key: value
                for key, value in candidate.metadata.items()
                if key
                not in {
                    "channel_rank",
                    "channel_score",
                    "hits",
                    "rrf_score",
                    "fused_rank",
                }
            }
            single_hit = hits[0] if len(hits) == 1 else None
            source_metadata = stable_metadata.get("source_metadata", {})
            if not isinstance(source_metadata, dict):
                source_metadata = {}
            fused_rank = (
                _positive_rank(candidate.metadata.get("fused_rank")) or rank
            )
            fused_score = (
                _score(candidate.metadata.get("rrf_score")) or candidate.score
            )
            traces.append(
                RetrievalCandidateTrace(
                    provider=self.name,
                    source_type="file_chunk",
                    source_id=candidate.source_id,
                    logical_source_id=str(
                        stable_metadata.get("attachment_id") or ""
                    )
                    or None,
                    revision_id=_optional_text(
                        source_metadata.get("revision_id")
                    ),
                    native_rank=single_hit.rank if single_hit else None,
                    native_score=single_hit.score if single_hit else None,
                    fused_rank=fused_rank,
                    fused_score=fused_score,
                    rerank_rank=_positive_rank(
                        candidate.metadata.get("rerank_rank")
                    ),
                    rerank_score=_score(
                        candidate.metadata.get("rerank_score")
                    ),
                    selected_for_result=selected,
                    filter_reason=filter_reason,
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

        channels = [
            RetrievalChannelTrace(
                channel="keyword",
                status=keyword_status,
                hit_count=len(keyword_chunks),
                error_message=keyword_error,
            ),
            RetrievalChannelTrace(
                channel="vector",
                status=vector_status,
                hit_count=len(vector_rows),
                error_message=vector_error,
            ),
        ]
        if self._reranker is not None:
            channels.append(
                RetrievalChannelTrace(
                    channel="rerank",
                    status=rerank_status,
                    hit_count=rerank_hit_count,
                    error_message=rerank_error,
                )
            )
        return ProviderResult(
            self.name,
            status,
            tuple(items),
            candidate_count=len(traces),
            result_count=len(items),
            duration_ms=round((time.perf_counter() - started) * 1000),
            candidates=tuple(traces),
            query=query,
            resource_scope={"attachment_ids": list(file_ids)},
            effective_params={
                "recall_limit": plan.recall_limit,
                "candidate_limit": channel_limit,
                "rerank_input_limit": rerank_input_limit,
                "rerank_output_limit": rerank_output_limit,
                "rerank_limit": plan.rerank_limit,
                "rerank_status": rerank_status,
                "rerank_min_score": self._rerank_min_score,
                "rerank_result_limit": self._rerank_result_limit,
                "result_limit": result_limit,
            },
            channels=tuple(channels),
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
        """将文件文本块转换为统一检索候选项。

        参数：
        - `source_id`（str）：当前候选或记录的来源标识；约束：无额外运行时限制。
        - `content`（str）：待处理或返回的正文内容；约束：无额外运行时限制。
        - `score`（float | None）：候选项在当前检索通道中的相关性分数；约束：允许为 None，按未提供值的分支处理。
        - `attachment_id`（str）：目标附件的稳定标识；约束：无额外运行时限制。
        - `locator`（dict[str, Any]）：定位来源内容在原始文件中的位置说明；约束：无额外运行时限制。
        - `rank`（int）：候选在所属检索通道中的名次；约束：无额外运行时限制。
        - `source_metadata`（dict[str, Any]）：候选来源携带的附加字段；约束：无额外运行时限制。

        返回：
        - RetrievalCandidate：统一格式的检索候选内容及其来源信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
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
        reranker: RetrievalRerankPort | None = None,
        candidate_limit: int | None = None,
        rerank_result_limit: int | None = None,
        rerank_min_score: float | None = None,
        limit: int = 5,
        timeout_seconds: float = 5.0,
    ) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `chunks`（FileChunkRepository）：按顺序读取的文件字节块异步迭代器；约束：无额外运行时限制。
        - `attachments`（AttachmentRepository）：当前请求关联或已校验的附件集合；约束：无额外运行时限制。
        - `vector_indexer`（FileVectorSearchPort | None）：执行向量检索或索引操作的可选依赖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `reranker`（RetrievalRerankPort | None）：对融合候选执行重排的可选能力；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `candidate_limit`（int | None）：单通道进入融合的候选上限；None 表示沿用计划上限；约束：可省略，默认值为 `None`。
        - `rerank_result_limit`（int | None）：重排后最多保留的候选数；None 表示沿用计划预算；约束：可省略，默认值为 `None`。
        - `rerank_min_score`（float | None）：无正关键词命中的候选须达到的重排分数门槛；None 表示不启用准入门槛。
        - `limit`（int）：本次读取或处理的数量上限；约束：可省略，默认值为 `5`。
        - `timeout_seconds`（float）：本次操作允许使用的最长秒数；约束：可省略，默认值为 `5.0`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        super().__init__(
            chunks,
            vector_indexer,
            reranker=reranker,
            candidate_limit=candidate_limit,
            rerank_result_limit=rerank_result_limit,
            rerank_min_score=rerank_min_score,
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
        """按计划检索文件或知识库上下文并生成候选项和通道追踪。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
        - `plan`（ContextPlan）：当前计划对象，提供请求的检索或执行安排；约束：无额外运行时限制。

        返回：
        - ProviderResult：检索提供方的状态、候选结果和追踪信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        knowledge_ids = plan.knowledge_base_ids
        if not knowledge_ids:
            return ProviderResult(
                self.name,
                "skipped",
                error_message="no_ready_knowledge_bases",
                query=plan.knowledge_query,
                resource_scope={
                    "knowledge_base_ids": [],
                    "ready_attachment_ids": [],
                },
            )
        documents = []
        for knowledge_id in knowledge_ids:
            documents.extend(
                await self._attachments.list_by_knowledge_base(knowledge_id)
            )
        ready_ids = tuple(
            item.id for item in documents if item.status == AttachmentStatus.READY
        )
        if not ready_ids:
            return ProviderResult(
                self.name,
                "skipped",
                error_message="no_ready_knowledge_bases",
                query=plan.knowledge_query,
                resource_scope={
                    "knowledge_base_ids": list(knowledge_ids),
                    "ready_attachment_ids": [],
                },
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
            replace(
                candidate,
                provider=self.name,
                source_type="knowledge_chunk",
                metadata={
                    **candidate.metadata,
                    "knowledge_base_ids": list(knowledge_ids),
                },
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
    keyword_error: str | None,
    vector_error: str | None,
    recall_limit: int,
    started: float,
) -> ProviderResult:
    """构造包含检索通道状态和追踪信息的文件检索失败结果。

    参数：
    - `provider`（str）：提供检索或执行能力的上下文提供方；约束：无额外运行时限制。
    - `status`（str）：当前操作或记录的状态值；约束：无额外运行时限制。
    - `error`（str）：操作失败时捕获的异常或错误说明；约束：无额外运行时限制。
    - `query`（str）：用于检索、匹配或解析的原始查询文本；约束：无额外运行时限制。
    - `file_ids`（tuple[str, ...]）：当前请求允许检索的文件标识集合；约束：无额外运行时限制。
    - `keyword_chunks`（list[Any]）：关键词检索返回的文件文本块；约束：无额外运行时限制。
    - `vector_rows`（list[dict[str, Any]]）：向量检索通道返回的原始行集合；约束：无额外运行时限制。
    - `allowed`（dict[str, Any]）：经过权限校验后允许参与检索的文件范围；约束：无额外运行时限制。
    - `keyword_status`（str）：关键词检索阶段的状态；约束：无额外运行时限制。
    - `vector_status`（str）：向量检索阶段的状态；约束：无额外运行时限制。
    - `keyword_error`（str | None）：关键词通道失败说明；None 表示无错误。
    - `vector_error`（str | None）：向量通道失败说明；None 表示无错误。
    - `recall_limit`（int）：本次召回阶段最多保留的候选数量；约束：无额外运行时限制。
    - `started`（float）：本阶段开始时记录的单调时钟值，用于计算剩余时限；约束：无额外运行时限制。

    返回：
    - ProviderResult：检索提供方的状态、候选结果和追踪信息。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
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
            error_message=keyword_error,
        )
    ]
    channels.append(
        RetrievalChannelTrace(
            channel="vector",
            status=vector_status,
            hit_count=len(vector_rows),
            error_message=vector_error,
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
    """将输入分数转换为有限、可比较的数值。

    参数：
    - `value`（Any）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - float | None：归一化后的有限相关性分数；输入缺失或非数值时返回 None。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _has_positive_keyword_hit(candidate: RetrievalCandidate) -> bool:
    """判断候选是否具有正分的精确关键词命中。"""
    hits = candidate.metadata.get("hits", ())
    if not isinstance(hits, (list, tuple)):
        return False
    for hit in hits:
        if not isinstance(hit, dict) or hit.get("channel") != "keyword":
            continue
        rank = _positive_rank(hit.get("rank"))
        score = _score(hit.get("score"))
        if rank is not None and score is not None and score > 0:
            return True
    return False


def _positive_rank(value: Any) -> int | None:
    """把追踪元数据中的名次规范化为正整数或 None。"""
    try:
        rank = int(value)
    except (TypeError, ValueError):
        return None
    return rank if rank >= 1 else None


def _optional_text(value: Any) -> str | None:
    """将可选输入规范化为非空文本或 None。

    参数：
    - `value`（Any）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - str | None：去除首尾空白的非空文本；输入不是字符串或为空时返回 None。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    text = str(value).strip() if value is not None else ""
    return text or None


__all__ = ["FileContextProvider", "KnowledgeContextProvider"]
