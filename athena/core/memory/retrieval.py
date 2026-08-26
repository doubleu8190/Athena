"""混合检索管理器 — LLM 查询扩展 + 向量检索 + 关键词检索 + RRF 融合 + 时间衰减.

技术方案：LLM 提取 + 向量数据库 + 混合检索。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any, Awaitable, Callable, Protocol

from langchain_core.messages import HumanMessage

from athena.config.settings import Settings
from athena.core.llm.provider import LLMProvider
from athena.core.llm.tokens import TokenCounter
from athena.core.memory.memory import MemoryManager
from athena.core.retrieval.trace import RetrievalTrace
from athena.utils.llm import extract_message_text
from athena.utils.logging import get_logger
from athena.utils.prompts import get_prompt

logger = get_logger(__name__)

# 记忆度加权系数（调参旋钮，非配置项）
_MEM_FREQ_CAP = 0.5  # 频率加成上限
_MEM_FREQ_K = 0.15  # log1p 缩放系数
_MEM_RECENCY_M = 0.4  # 新近度加成上限


@dataclass
class SearchResult:
    """检索结果."""

    content: str
    score: float
    source: str  # "vector" / "keyword" / "fused"（向量 / 关键词 / 融合）
    metadata: dict[str, Any]
    chunk_id: str


class ShadowSubmitter(Protocol):
    """应用层 Shadow runner 的最小依赖，避免 core 依赖评估实现。"""

    async def submit(
        self,
        *,
        query: str,
        scope: dict[str, Any],
        baseline_ids: list[str],
        labels: list[str] | None = None,
    ) -> bool: ...


class HybridRetrievalManager:
    """混合检索管理器.

    流程：LLM 查询扩展 → 向量检索 + 关键词检索 → RRF 融合 → 时间衰减 → 过滤排序.
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        memory_manager: MemoryManager,
        settings: Settings,
        trace_sink: Callable[[RetrievalTrace], None] | None = None,
    ) -> None:
        """初始化当前对象。

        参数：
            llm_provider (LLMProvider): 输入参数；其类型和取值约束由方法签名及实现定义。
            memory_manager (MemoryManager): 输入参数；其类型和取值约束由方法签名及实现定义。
            settings (Settings): 全局配置对象。
            trace_sink (Callable[[RetrievalTrace], None] | None): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self._llm = llm_provider
        self._memory = memory_manager
        self._settings = settings
        self._min_score = self._settings.memory_min_score
        self._top_k = self._settings.retrieval_top_k
        self._vector_weight = self._settings.vector_weight
        self._keyword_weight = self._settings.keyword_weight
        self._rrf_k = self._settings.rrf_k
        self._ttl_days = self._settings.memory_ttl_days
        self._trace_enabled = self._settings.retrieval_trace_enabled
        self._trace_sink = trace_sink

    async def retrieve(
        self,
        query: str,
        filter_params: dict[str, Any] | None = None,
        *,
        record_access: bool = True,
    ) -> list[SearchResult]:
        """执行混合检索（默认跨会话全库）.

        长期记忆定位为跨会话召回：不按 session_id 过滤，任何会话沉淀的
        记忆都可被召回；filter_params 为可选显式过滤（按需传入 category/
        type 等），产线不传即全库检索。
        """
        trace = self._new_trace(query)
        selected: list[SearchResult] = []
        try:
            # 1. LLM 查询扩展
            expanded_query = await self._expand_query(query, trace)

            # 2. 向量检索
            vector_results = await self._vector_search(
                expanded_query or query, filter_params, trace, record_access
            )

            # 3. 关键词检索
            keyword_results = await self._keyword_search(
                query, filter_params, trace, record_access
            )

            # 4. RRF 融合
            fusion_started = perf_counter()
            fused = self._reciprocal_rank_fusion(vector_results, keyword_results)
            if trace is not None:
                trace.set_fused_scores(
                    {result.chunk_id: result.score for result in fused}
                )
                trace.add_stage(
                    stage="fusion",
                    route="rrf",
                    query_index=0,
                    duration_ms=(perf_counter() - fusion_started) * 1000,
                    result_count=len(fused),
                )

            # 5. 记忆度叠加 + 时间衰减 + 过滤 + 排序
            # 未落盘的访问统计先叠加进 metadata，使加权实时生效（不等同步周期）
            selection_started = perf_counter()
            pending = self._memory.pending_access_stats(r.chunk_id for r in fused)
            for r in fused:
                meta = r.metadata
                if r.chunk_id in pending:
                    extra, last = pending[r.chunk_id]
                    meta = {
                        **meta,
                        "access_count": (int(meta.get("access_count") or 0) + extra),
                        "last_accessed": last,
                    }
                r.score *= self._calculate_temporal_decay(meta)
                r.score *= self._calculate_memorability(meta)

            # RRF 分数上界约 2/(rrf_k+1)，将原语义阈值映射到该尺度。
            # 融合后只保留证据足够强的记忆，避免弱命中污染上下文。
            # 纯关键词命中通常不能单独跨线；这是现有 legacy 行为，PR-02 才调整。
            threshold = self._min_score / (self._rrf_k + 1)
            filtered = [r for r in fused if r.score >= threshold]
            filtered.sort(key=lambda x: x.score, reverse=True)
            selected = filtered[: self._top_k]
            if trace is not None:
                trace.add_stage(
                    stage="selection",
                    route="memory",
                    query_index=0,
                    duration_ms=(perf_counter() - selection_started) * 1000,
                    result_count=len(selected),
                )
            return selected
        finally:
            if trace is not None:
                trace.mark_selected([result.chunk_id for result in selected])
                trace.finish()
                self._emit_trace(trace)

    async def _expand_query(
        self, query: str, trace: RetrievalTrace | None = None
    ) -> str:
        """使用 LLM 扩展查询.

        扩展结果仅用于向量语义检索（关键词检索使用原始查询走 FTS5），
        因此提示词明确语义检索目标、约束输出为单行语句。
        """
        started = perf_counter()
        prompt = get_prompt("query_expansion").format(query=query)
        try:
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            expanded = extract_message_text(response).strip()
            if not expanded:
                # 空响应（模型返回工具调用/错误/无文本）静默回落为 "None" 曾导致
                # 下游检索被垃圾查询污染且无日志，这里显式记录并回退原查询。
                logger.warning("query_expand_empty", query_length=len(query))
                if trace is not None:
                    trace.add_fallback("query_expand_empty")
                    trace.add_stage(
                        stage="query_expand",
                        route="llm",
                        query_index=0,
                        duration_ms=(perf_counter() - started) * 1000,
                        result_count=0,
                        error_code="query_expand_empty",
                    )
                return query
            if trace is not None:
                trace.add_stage(
                    stage="query_expand",
                    route="llm",
                    query_index=0,
                    duration_ms=(perf_counter() - started) * 1000,
                    result_count=1,
                )
            return expanded
        except Exception as e:
            logger.warning("query_expand_failed", error=str(e))
            if trace is not None:
                trace.add_fallback("query_expand_failed")
                trace.add_stage(
                    stage="query_expand",
                    route="llm",
                    query_index=0,
                    duration_ms=(perf_counter() - started) * 1000,
                    result_count=0,
                    error_code="query_expand_failed",
                )
            return query

    async def _vector_search(
        self,
        query: str,
        filter_params: dict[str, Any] | None,
        trace: RetrievalTrace | None = None,
        record_access: bool = True,
    ) -> list[SearchResult]:
        """向量检索（跨会话全库；filter_params 为可选显式过滤）."""
        started = perf_counter()
        try:
            kwargs: dict[str, Any] = {
                "query": query,
                "n_results": self._top_k * 2,
                "where": filter_params,
            }
            if not record_access:
                kwargs["record_access"] = False
            results = await self._memory.search(**kwargs)
        except Exception as e:
            logger.warning("vector_search_failed", error=str(e))
            if trace is not None:
                trace.add_fallback("vector_search_failed")
                trace.add_stage(
                    stage="candidate_retrieval",
                    route="vector",
                    query_index=0,
                    duration_ms=(perf_counter() - started) * 1000,
                    result_count=0,
                    error_code="vector_search_failed",
                )
            return []

        out: list[SearchResult] = []
        for i, r in enumerate(results, 1):
            out.append(
                SearchResult(
                    content=r["content"],
                    score=r["score"],
                    source="vector",
                    metadata=r.get("metadata", {}),
                    chunk_id=r.get("id", str(i)),
                )
            )
        # RRF 依赖列表位置作为 rank（位置越靠前贡献越大），必须"最相关在前"。
        # score 为原始相似度 1-dist/2（越大越相似），vector_weight 统一在 RRF
        # 融合处生效，这里不乘权重，避免融合改读 r.score 时双倍加权。上游
        # ChromaDB 已按距离升序返回，这里显式按 score 降序把不变量固化。
        out.sort(key=lambda x: x.score, reverse=True)
        if trace is not None:
            trace.add_stage(
                stage="candidate_retrieval",
                route="vector",
                query_index=0,
                duration_ms=(perf_counter() - started) * 1000,
                result_count=len(out),
            )
            for rank, result in enumerate(out, 1):
                trace.add_candidate(
                    item_id=result.chunk_id,
                    route="vector",
                    rank=rank,
                    native_score=result.score,
                )
        return out

    async def _keyword_search(
        self,
        query: str,
        filter_params: dict[str, Any] | None,
        trace: RetrievalTrace | None = None,
        record_access: bool = True,
    ) -> list[SearchResult]:
        """关键词检索（SQLite FTS5 MATCH 全文检索）.

        通过 MemoryManager.keyword_search() 走 FTS5 虚拟表的 MATCH 操作符，
        tokenize='unicode61' 仅做精确词/整段/前缀召回，不做中文语义分词
        （分词语义由向量检索承担），bm25 算法排序。

        跨会话全库检索：不再按 session_id 过滤，跨会话的关键词命中也参与
        RRF 融合；filter_params 为可选显式过滤（仅支持 memories 表顶层列）。
        """
        started = perf_counter()
        try:
            kwargs = {
                "query": query,
                "n_results": self._top_k * 2,
                "where": filter_params,
            }
            if not record_access:
                kwargs["record_access"] = False
            results = await self._memory.keyword_search(**kwargs)
        except Exception as e:
            logger.warning("keyword_search_failed", error=str(e))
            if trace is not None:
                trace.add_fallback("keyword_search_failed")
                trace.add_stage(
                    stage="candidate_retrieval",
                    route="keyword",
                    query_index=0,
                    duration_ms=(perf_counter() - started) * 1000,
                    result_count=0,
                    error_code="keyword_search_failed",
                )
            return []

        out: list[SearchResult] = []
        for i, r in enumerate(results, 1):
            out.append(
                SearchResult(
                    content=r["content"],
                    score=r["score"],
                    source="keyword",
                    metadata=r.get("metadata", {}),
                    chunk_id=r.get("id", str(i)),
                )
            )
        # 同上：RRF 需要"最相关在前"。score 为原始相似度 |bm25|/(1+|bm25|)
        # （越大越相关），keyword_weight 统一在 RRF 融合处生效。上游已
        # ORDER BY rank(=bm25 升序) 返回，这里显式按 score 降序固化不变量。
        out.sort(key=lambda x: x.score, reverse=True)
        if trace is not None:
            trace.add_stage(
                stage="candidate_retrieval",
                route="keyword",
                query_index=0,
                duration_ms=(perf_counter() - started) * 1000,
                result_count=len(out),
            )
            for rank, result in enumerate(out, 1):
                trace.add_candidate(
                    item_id=result.chunk_id,
                    route="keyword",
                    rank=rank,
                    native_score=result.score,
                )
        return out

    def _new_trace(
        self,
        query: str,
    ) -> RetrievalTrace | None:
        """执行“new trace”操作。

        参数：
            query (str): 检索或搜索文本；应为非空字符串。
        返回值：
            RetrievalTrace | None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        if not self._trace_enabled:
            return None
        return RetrievalTrace(
            source_scope="memory",
            query=query,
            query_hash_salt=self._settings.retrieval_trace_query_hash_salt,
            include_raw_query=self._settings.retrieval_trace_include_raw_query,
            query_labels=["memory"],
        )

    def _emit_trace(self, trace: RetrievalTrace) -> None:
        """执行“emit trace”操作。

        参数：
            trace (RetrievalTrace): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        try:
            if self._trace_sink is not None:
                self._trace_sink(trace)
            logger.info("retrieval_completed", **trace.as_log_fields())
        except Exception:
            logger.warning("retrieval_trace_emit_failed")

    def _reciprocal_rank_fusion(
        self,
        vector_results: list[SearchResult],
        keyword_results: list[SearchResult],
    ) -> list[SearchResult]:
        """使用加权倒数排名融合向量和关键词候选集。

        每条检索路只贡献排名证据，不直接比较不同路由的原始分数，因而
        可以在向量距离和 BM25 分数尺度不同的情况下保持排序稳定。
        """
        scores: dict[str, float] = {}
        content_map: dict[str, SearchResult] = {}

        # 加权 RRF：按来源权重缩放贡献，使 vector_weight / keyword_weight 真正生效。
        # 首次出现时保留完整结果对象，后续只更新融合分数，避免重复候选携带不一致内容。
        # （职责边界：向量路承担语义，关键词路仅作精确词/前缀助力的弱贡献）
        for rank, r in enumerate(vector_results, 1):
            if r.chunk_id not in scores:
                scores[r.chunk_id] = 0.0
                content_map[r.chunk_id] = r
            scores[r.chunk_id] += self._vector_weight / (self._rrf_k + rank)

        for rank, r in enumerate(keyword_results, 1):
            if r.chunk_id not in scores:
                scores[r.chunk_id] = 0.0
                content_map[r.chunk_id] = r
            scores[r.chunk_id] += self._keyword_weight / (self._rrf_k + rank)

        fused: list[SearchResult] = []
        for chunk_id, rrf_score in scores.items():
            result = content_map[chunk_id]
            result.score = rrf_score
            result.source = "fused"
            fused.append(result)
        return fused

    def _calculate_temporal_decay(self, metadata: dict[str, Any]) -> float:
        """计算时间衰减因子."""
        created_at = metadata.get("created_at")
        if not created_at:
            return 1.0
        try:
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
        last = metadata.get("last_accessed")
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
    """记忆检索服务 - 对外接口，将检索结果格式化为系统提示."""

    def __init__(
        self,
        retrieval_manager: HybridRetrievalManager,
        token_counter: TokenCounter,
        settings: Settings,
        shadow_runner: ShadowSubmitter | None = None,
    ) -> None:
        """初始化当前对象。

        参数：
            retrieval_manager (HybridRetrievalManager): 输入参数；其类型和取值约束由方法签名及实现定义。
            token_counter (TokenCounter): 输入参数；其类型和取值约束由方法签名及实现定义。
            settings (Settings): 全局配置对象。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self._manager = retrieval_manager
        self._token_counter = token_counter
        self._max_tokens = settings.memory_max_tokens
        self._shadow_runner = shadow_runner

    async def get_relevant_memories(
        self,
        user_message: str,
    ) -> str:
        """获取相关记忆并格式化为系统提示（跨会话召回）."""
        try:
            results = await self._manager.retrieve(
                query=user_message,
            )
            if self._shadow_runner is not None:
                try:
                    await self._shadow_runner.submit(
                        query=user_message,
                        scope={"source": "memory"},
                        baseline_ids=[result.chunk_id for result in results],
                        labels=["memory"],
                    )
                except Exception:
                    logger.warning("memory_shadow_submit_failed")
        except Exception as e:
            logger.warning("memory_retrieve_failed", error=str(e))
            return ""

        if not results:
            return ""

        parts = ["[相关记忆]"]
        total_tokens = 0
        for r in results:
            content_tokens = self._token_counter.count_text_tokens(r.content)
            if total_tokens + content_tokens > self._max_tokens:
                break
            parts.append(f"- {r.content}")
            total_tokens += content_tokens

        if len(parts) > 1:
            parts.append("[/相关记忆]")
            return "\n".join(parts)
        return ""
