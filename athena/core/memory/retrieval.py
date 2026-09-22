"""混合检索管理器 — 向量检索 + 关键词检索 + RRF 融合 + 时间衰减.

技术方案：LLM 提取 + 向量数据库 + 混合检索。
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from athena.config.settings import Settings
from athena.core.llm.tokens import TokenCounter
from athena.core.memory.long_term_memory import LongTermMemoryService
from athena.core.memory.contracts import MemoryRetrievalRequest
from athena.core.retrieval import RetrievalCandidate, RetrievalRunRequest
from athena.core.retrieval.ports import RetrievalTraceWriter
from athena.utils.logging import get_logger

logger = get_logger(__name__)

# 记忆度加权系数（调参旋钮，非配置项）
_MEM_FREQ_CAP = 0.5  # 频率加成上限
_MEM_FREQ_K = 0.15  # log1p 缩放系数
_MEM_RECENCY_M = 0.4  # 新近度加成上限


@dataclass
class MemoryRetrievalResult:
    """长期记忆检索结果，保存各检索阶段的分数。"""

    content: str
    memory_id: str
    source: str = "vector"
    metadata: dict[str, Any] = field(default_factory=dict)
    native_score: float | None = None
    fused_score: float | None = None
    rerank_score: float | None = None
    native_rank: int | None = None
    fused_rank: int | None = None
    retrieval_run_id: str | None = None
    filter_reason: str | None = None
    exact_match: bool = False


class HybridMemoryRetriever:
    """混合检索管理器.

    流程：向量检索 + 关键词检索 → RRF 融合 → 时间衰减 → 过滤排序.
    """

    def __init__(
        self,
        memory_service: LongTermMemoryService,
        settings: Settings,
        trace_writer: RetrievalTraceWriter | None = None,
    ) -> None:
        """

        参数：
            memory_service (LongTermMemoryService): 提供向量和关键词检索的长期记忆服务。
            settings (Settings): 全局配置对象。
        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        self._memory = memory_service
        self._settings = settings
        self._trace_writer = trace_writer
        self._min_score = self._settings.memory_min_score
        self._top_k = self._settings.retrieval_top_k
        self._candidate_k = self._settings.retrieval_candidate_k or self._top_k * 2

        self._rerank_k = self._settings.retrieval_rerank_k or self._top_k
        self._context_k = self._settings.retrieval_context_k or self._top_k
        self._vector_min_score = (
            self._settings.memory_vector_min_score or self._min_score
        )
        self._vector_weight = self._settings.vector_weight
        self._keyword_weight = self._settings.keyword_weight
        self._rrf_k = self._settings.rrf_k
        self._ttl_days = self._settings.memory_ttl_days

    def record_selected_access(self, memory_ids: Iterable[str]) -> None:
        """记录已写入上下文的记忆访问。"""
        self._memory.record_selected_access(memory_ids)

    async def _complete_trace(
        self,
        run_id: str | None,
        started: float,
        *,
        candidate_count: int,
        selected_count: int = 0,
        status: str,
        error_message: str | None = None,
    ) -> None:
        """写入召回终态，避免检索异常留下未完成运行。

        参数：
            run_id: 召回运行标识；为空时不执行写入。
            started: ``perf_counter`` 记录的起始时间。
            candidate_count: 融合阶段候选数量。
            selected_count: 通过 provider 预算的候选数量。
            status: 终态，例如 succeeded、partial、failed 或 cancelled。
            error_message: 可选的错误信息。

        返回值：
            None。

        异常：
            轨迹写入失败只记录日志，不影响原始召回结果。
        """
        if run_id is None or self._trace_writer is None:
            return
        try:
            await self._trace_writer.complete_run(
                run_id,
                candidate_count=candidate_count,
                selected_count=selected_count,
                status=status,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                error_message=error_message,
            )
        except Exception as exc:
            logger.warning("retrieval_trace_complete_failed", error=str(exc))

    async def retrieve(
        self,
        query: str,
        filter_params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        agent_run_id: str | None = None,
        message_id: str | None = None,
    ) -> list[MemoryRetrievalResult]:
        """执行混合检索（默认跨会话全库）.

        长期记忆定位为跨会话召回：不按 session_id 过滤，任何会话沉淀的
        记忆都可被召回；filter_params 为可选显式过滤（按需传入 category/
        type 等），产线不传即全库检索。
        """
        started = time.perf_counter()
        run_id: str | None = None
        if self._trace_writer is not None:
            try:
                run_id = await self._trace_writer.start_run(
                    RetrievalRunRequest(
                        query=query,
                        scope="memory",
                        config={
                            "candidate_k": self._candidate_k,
                            "rerank_k": self._rerank_k,
                            "context_k": self._context_k,
                            "vector_min_score": self._vector_min_score,
                            "vector_weight": self._vector_weight,
                            "keyword_weight": self._keyword_weight,
                            "rrf_k": self._rrf_k,
                        },
                        index_generation="memory/current",
                        session_id=session_id,
                        agent_run_id=agent_run_id,
                        message_id=message_id,
                    )
                )
            except Exception as exc:
                logger.warning("retrieval_trace_start_failed", error=str(exc))

        try:
            # 并发执行向量和关键词路线，随后统一使用 RRF 合并两路排名证据。
            keyword_task = asyncio.create_task(self._keyword_search(query, filter_params))
            raw_vector_task = asyncio.create_task(self._vector_search(query, filter_params))

            keyword_results = await keyword_task
            vector_results = await raw_vector_task

            logger.info(
                "memory_retrieval_completed",
                duration_ms=round((time.perf_counter() - started) * 1000),
                keyword_count=len(keyword_results),
                vector_count=len(vector_results),
            )

            fused = self._reciprocal_rank_fusion(
                [result for result in vector_results if result.filter_reason is None],
                keyword_results,
            )

            # 生命周期信号只作为同一相关度层内的 tie-break。
            for r in fused:
                r.rerank_score = r.fused_score
            fused.sort(
                key=lambda x: (
                    0 if x.exact_match else 1,
                    -float(x.fused_score or 0.0),
                    -self._lifecycle_score(x.metadata),
                    x.memory_id,
                )
            )
            for rank, result in enumerate(fused, 1):
                result.fused_rank = rank
            selected = fused[: self._rerank_k][: self._context_k]
            if run_id is not None and self._trace_writer is not None:
                try:
                    raw_candidates = [
                        self._trace_candidate(result, provider="vector", stage="native")
                        for result in vector_results
                    ]
                    raw_candidates.extend(
                        self._trace_candidate(result, provider="keyword", stage="native")
                        for result in keyword_results
                    )
                    selected_ids = {result.memory_id for result in selected}
                    fused_candidates = [
                        self._trace_candidate(
                            result,
                            provider="fusion",
                            stage="fused",
                            selected_for_result=result.memory_id in selected_ids,
                        )
                        for result in fused
                    ]
                    await self._trace_writer.record_candidates(
                        run_id, [*raw_candidates, *fused_candidates]
                    )
                    await self._complete_trace(
                        run_id,
                        started,
                        candidate_count=len(fused),
                        selected_count=len(selected),
                        status="succeeded",
                    )
                except Exception as exc:
                    logger.warning("retrieval_trace_record_failed", error=str(exc))
                    await self._complete_trace(
                        run_id,
                        started,
                        candidate_count=len(fused),
                        selected_count=len(selected),
                        status="partial",
                        error_message=str(exc),
                    )
            for result in selected:
                result.retrieval_run_id = run_id
            return selected
        except asyncio.CancelledError:
            await self._complete_trace(
                run_id,
                started,
                candidate_count=0,
                status="cancelled",
                error_message="检索任务被取消",
            )
            raise
        except Exception as exc:
            await self._complete_trace(
                run_id,
                started,
                candidate_count=0,
                status="failed",
                error_message=str(exc),
            )
            raise

    @staticmethod
    def _trace_candidate(
        result: MemoryRetrievalResult,
        *,
        provider: str,
        stage: str,
        selected_for_result: bool = False,
    ) -> RetrievalCandidate:
        """把记忆检索结果转换为可持久化的候选轨迹。"""
        metadata = result.metadata
        filter_reason = result.filter_reason
        if stage == "fused" and not selected_for_result and filter_reason is None:
            filter_reason = "outside_context_k"
        return RetrievalCandidate(
            provider=provider,
            stage=stage,
            source_type="memory_revision",
            source_id=result.memory_id,
            native_rank=result.native_rank,
            native_score=result.native_score,
            fused_rank=result.fused_rank if stage == "fused" else None,
            fused_score=result.fused_score if stage == "fused" else None,
            logical_source_id=metadata.get("logical_memory_id"),
            revision_id=result.memory_id,
            filter_reason=filter_reason,
            selected_for_result=selected_for_result,
            content_preview=result.content[:500],
            metadata={"source": result.source, "exact_match": result.exact_match},
        )

    async def _vector_search(
        self,
        query: str,
        filter_params: dict[str, Any] | None,
    ) -> list[MemoryRetrievalResult]:
        """向量检索（跨会话全库；filter_params 为可选显式过滤）."""
        try:
            kwargs: dict[str, Any] = {
                "query": query,
                "n_results": self._candidate_k,
                "where": filter_params,
            }
            results = await self._memory.search(**kwargs)
        except Exception as e:
            logger.warning("vector_search_failed", error=str(e))
            return []

        out: list[MemoryRetrievalResult] = []
        for i, r in enumerate(results, 1):
            score = r.get("score")
            out.append(
                MemoryRetrievalResult(
                    content=r["content"],
                    source="vector",
                    metadata=r.get("metadata", {}),
                    native_score=score,
                    native_rank=i,
                    memory_id=r.get("id", str(i)),
                    filter_reason=(
                        None
                        if (score or 0.0) >= self._vector_min_score
                        else "below_vector_min_score"
                    ),
                )
            )
        # score 为原始相似度 1-dist/2（越大越相似），这里显式按 score 降序。
        out.sort(key=lambda x: x.native_score or 0.0, reverse=True)
        for rank, result in enumerate(out, 1):
            result.native_rank = rank
        return out

    async def _keyword_search(
        self,
        query: str,
        filter_params: dict[str, Any] | None,
    ) -> list[MemoryRetrievalResult]:
        """关键词检索（SQLite FTS5 MATCH 全文检索）.

        通过 LongTermMemoryService.keyword_search() 走 FTS5 虚拟表的 MATCH 操作符，
        tokenize='unicode61' 仅做精确词/整段/前缀召回，不做中文语义分词
        （分词语义由向量检索承担），bm25 算法排序。

        跨会话全库检索：不再按 session_id 过滤，跨会话的关键词命中也参与
        RRF 融合；filter_params 为可选显式过滤（仅支持 memories 表顶层列）。
        """
        try:
            kwargs = {
                "query": query,
                "n_results": self._candidate_k,
                "where": filter_params,
            }
            results = await self._memory.keyword_search(**kwargs)
        except Exception as e:
            logger.warning("keyword_search_failed", error=str(e))
            return []

        out: list[MemoryRetrievalResult] = []
        for i, r in enumerate(results, 1):
            out.append(
                MemoryRetrievalResult(
                    content=r["content"],
                    source="keyword",
                    metadata=r.get("metadata", {}),
                    native_score=r.get("score"),
                    native_rank=i,
                    exact_match=self._is_exact_match(query, r["content"]),
                    memory_id=r.get("id", str(i)),
                )
            )
        # 同上：RRF 需要"最相关在前"。score 为原始相似度 |bm25|/(1+|bm25|)
        # （越大越相关），keyword_weight 统一在 RRF 融合处生效。上游已
        # ORDER BY rank(=bm25 升序) 返回，这里显式按 score 降序固化不变量。
        out.sort(key=lambda x: x.native_score or 0.0, reverse=True)
        for rank, result in enumerate(out, 1):
            result.native_rank = rank
        return out

    def _reciprocal_rank_fusion(
        self,
        vector_results: list[MemoryRetrievalResult],
        keyword_results: list[MemoryRetrievalResult],
    ) -> list[MemoryRetrievalResult]:
        """使用加权倒数排名融合向量和关键词候选集。

        每条检索路只贡献排名证据，不直接比较不同路由的原始分数，因而
        可以在向量距离和 BM25 分数尺度不同的情况下保持排序稳定。
        """
        scores: dict[str, float] = {}
        content_map: dict[str, MemoryRetrievalResult] = {}

        # 加权 RRF：按来源权重缩放贡献，使 vector_weight / keyword_weight 真正生效。
        # 首次出现时保留完整结果对象，后续只更新融合分数，避免重复候选携带不一致内容。
        # （职责边界：向量路承担语义，关键词路仅作精确词/前缀助力的弱贡献）
        # 同一记忆可能同时命中两条路线；合并元数据时补齐先出现结果缺失的字段。
        for rank, r in enumerate(vector_results, 1):
            if r.memory_id not in scores:
                scores[r.memory_id] = 0.0
                content_map[r.memory_id] = r
            else:
                content_map[r.memory_id].metadata = {
                    **r.metadata,
                    **content_map[r.memory_id].metadata,
                }
            scores[r.memory_id] += self._vector_weight / (self._rrf_k + rank)

        for rank, r in enumerate(keyword_results, 1):
            if r.memory_id not in scores:
                scores[r.memory_id] = 0.0
                content_map[r.memory_id] = r
            else:
                content_map[r.memory_id].metadata = {
                    **r.metadata,
                    **content_map[r.memory_id].metadata,
                }
            content_map[r.memory_id].exact_match = (
                content_map[r.memory_id].exact_match or r.exact_match
            )
            scores[r.memory_id] += self._keyword_weight / (self._rrf_k + rank)

        fused: list[MemoryRetrievalResult] = []
        for memory_id, rrf_score in scores.items():
            result = content_map[memory_id]
            result.fused_score = rrf_score
            result.source = "fused"
            fused.append(result)
        return fused

    def _lifecycle_score(self, metadata: dict[str, Any]) -> float:
        """计算不改变相关度语义的次级生命周期信号。

        参数:
            metadata (dict[str, Any]): 记忆的生命周期元数据。
        返回值:
            float: 时间衰减与记忆度的乘积，用作同分结果的排序依据。
        异常:
            元数据格式异常时由内部计算逻辑传播相应异常。
        """
        return self._calculate_temporal_decay(metadata) * self._calculate_memorability(
            metadata
        )

    @staticmethod
    def _is_exact_match(query: str, content: str) -> bool:
        """判断是否命中完整词项，避免把任意子串误判为精确命中。

        参数:
            query (str): 用户查询文本。
            content (str): 候选内容文本。
        返回值:
            bool: 查询是完整内容或以词边界出现时返回 ``True``。
        异常:
            不抛出业务异常。
        """
        needle = query.strip().casefold()
        haystack = content.casefold()
        if not needle:
            return False
        if needle == haystack.strip():
            return True
        boundary = r"[\w\u4e00-\u9fff]"
        return (
            re.search(
                rf"(?<!{boundary}){re.escape(needle)}(?!{boundary})",
                haystack,
            )
            is not None
        )

    def _calculate_temporal_decay(self, metadata: dict[str, Any]) -> float:
        """计算时间衰减因子."""
        created_at = metadata.get("created_at")
        if not created_at:
            return 1.0
        try:
            if metadata.get("pinned"):
                return 1.0
            created = datetime.fromisoformat(created_at)
            age_days = (datetime.now() - created).days
            decay = max(0.1, 1.0 - (age_days / max(self._ttl_days, 1)))
            return decay
        except (ValueError, TypeError):
            return 1.0

    def _calculate_memorability(self, metadata: dict[str, Any]) -> float:
        """计算记忆度因子（recency × frequency），恒 ≥ 1，未访问恰为 1.0.

        频繁且近期被召回的活跃记忆获得加成，补偿创建时间衰减的惩罚；
        对纯语义排序的影响通过封顶约束（组合上限约 2.1×）。
        """
        try:
            count = int(metadata.get("access_count") or 0)
        except (ValueError, TypeError):
            count = 0
        freq = 1.0
        if count > 0:
            # 对数压缩：使用 math.log1p(count)（即 ln(count+1)）来平滑访问次数的影响，避免线性增长导致高频记忆因子过大。
            # 系数与封顶：乘以 _MEM_FREQ_K 控制频率的权重，再用 _MEM_FREQ_CAP 截断上限，防止极端高频记忆过度影响语义排序。
            # 结果：freq 在 [1.0, 1.0 + _MEM_FREQ_CAP] 区间内。
            freq = 1.0 + min(_MEM_FREQ_CAP, math.log1p(count) * _MEM_FREQ_K)

        recency = 1.0
        last = metadata.get("last_accessed_at")
        if last:
            try:
                # 当记忆刚刚被访问（days_since = 0）时，recency = 1.0 + _MEM_RECENCY_M，获得最大加成。
                # 当记忆的访问时间超过时间窗口（days_since ≥ window）时，recency 回退至 1.0（无加成）。
                # 在窗口内，加成线性衰减，_MEM_RECENCY_M 决定了衰减的起点高度。
                days_since = (datetime.now() - datetime.fromisoformat(last)).days
                window = self._settings.memory_access_window_days
                if 0 <= days_since < window:
                    recency = 1.0 + (1.0 - days_since / window) * _MEM_RECENCY_M
            except (ValueError, TypeError):
                pass
        return freq * recency


class MemoryRetrievalService:
    """记忆检索服务，将预算筛选后的结构化结果交给上下文 Provider。"""

    def __init__(
        self,
        retrieval_manager: HybridMemoryRetriever,
        token_counter: TokenCounter,
        settings: Settings,
    ) -> None:
        """

        参数：
            retrieval_manager (HybridMemoryRetriever): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            token_counter (TokenCounter): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            settings (Settings): 全局配置对象。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        self._manager = retrieval_manager
        self._token_counter = token_counter
        self._max_tokens = settings.memory_max_tokens

    async def get_context(
        self, request: MemoryRetrievalRequest
    ) -> list[MemoryRetrievalResult]:
        """获取经过上下文预算筛选的记忆检索结果。

        参数：
            request (MemoryRetrievalRequest): 包含查询文本和结果数量上限的请求。

        返回值：
            list[MemoryRetrievalResult]: 可注入运行时上下文的结构化记忆结果。

        异常：
            不向调用方传播检索异常；底层失败时返回空列表。
        """
        try:
            trace_kwargs = {
                key: value
                for key, value in {
                    "session_id": request.session_id,
                    "agent_run_id": request.agent_run_id,
                    "message_id": request.message_id,
                }.items()
                if value is not None
            }
            results = await self._manager.retrieve(query=request.query, **trace_kwargs)
        except Exception as e:
            logger.warning("memory_retrieve_failed", error=str(e))
            return []
        return self._select_results(
            results[: request.limit], record_access=request.record_access
        )

    def _select_results(
        self,
        results: list[MemoryRetrievalResult],
        *,
        record_access: bool = True,
    ) -> list[MemoryRetrievalResult]:
        """按 token 预算筛选结果，并记录实际注入上下文的记忆访问。

        参数：
            results (list[MemoryRetrievalResult]): 已按相关度排序的候选记忆。

        返回值：
            list[MemoryRetrievalResult]: 在预算内保留的结构化记忆结果。

        异常：
            不主动抛出业务异常；token 计数器异常由调用方边界处理。
        """
        if not results:
            return []
        total_tokens = 0
        selected_ids: list[str] = []
        selected: list[MemoryRetrievalResult] = []
        for result in results:
            content_tokens = self._token_counter.count_text_tokens(result.content)
            if total_tokens + content_tokens > self._max_tokens:
                # 单条记忆过长时跳过它，继续尝试后续短记忆，避免浪费整个上下文预算。
                continue
            total_tokens += content_tokens
            selected_ids.append(result.memory_id)
            selected.append(result)
        if not selected:
            return []
        if record_access:
            self._manager.record_selected_access(selected_ids)
        return selected

    def record_selected_access(self, memory_ids: Iterable[str]) -> None:
        """记录最终进入上下文的记忆访问。

        参数：
            memory_ids: 已通过最终上下文预算的记忆 revision ID。

        返回值：
            None。

        异常：
            底层记忆服务不支持访问统计时传播属性错误。
        """
        self._manager.record_selected_access(memory_ids)
