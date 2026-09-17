"""混合检索管理器 — LLM 查询扩展 + 向量检索 + 关键词检索 + RRF 融合 + 时间衰减.

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

from langchain_core.messages import HumanMessage

from athena.config.settings import Settings
from athena.core.llm.provider import LLMProvider
from athena.core.llm.tokens import TokenCounter
from athena.core.memory.long_term_memory import LongTermMemoryService
from athena.core.memory.contracts import MemoryRetrievalRequest
from athena.utils.llm import extract_message_text
from athena.utils.logging import get_logger
from athena.utils.prompts import get_prompt

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
    exact_match: bool = False
    rank_sources: dict[str, int] = field(default_factory=dict)


class HybridMemoryRetriever:
    """混合检索管理器.

    流程：LLM 查询扩展 → 向量检索 + 关键词检索 → RRF 融合 → 时间衰减 → 过滤排序.
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        memory_service: LongTermMemoryService,
        settings: Settings,
    ) -> None:
        """

        参数：
            llm_provider (LLMProvider): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            memory_service (LongTermMemoryService): 提供向量和关键词检索的长期记忆服务。
            settings (Settings): 全局配置对象。
        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        self._llm = llm_provider
        self._memory = memory_service
        self._settings = settings
        self._min_score = self._settings.memory_min_score
        self._top_k = self._settings.retrieval_top_k
        self._candidate_k = getattr(
            self._settings, "retrieval_candidate_k", self._top_k * 2
        )
        self._rerank_k = getattr(self._settings, "retrieval_rerank_k", self._top_k)
        self._context_k = getattr(self._settings, "retrieval_context_k", self._top_k)
        self._vector_min_score = getattr(
            self._settings, "memory_vector_min_score", self._min_score
        )
        self._vector_weight = self._settings.vector_weight
        self._keyword_weight = self._settings.keyword_weight
        self._rrf_k = self._settings.rrf_k
        self._ttl_days = self._settings.memory_ttl_days

    def record_selected_access(self, memory_ids: Iterable[str]) -> None:
        """记录已写入上下文的记忆访问。"""
        self._memory.record_selected_access(memory_ids)

    async def retrieve(
        self,
        query: str,
        filter_params: dict[str, Any] | None = None,
        *,
        record_access: bool = True,
        expand_query: bool = True,
    ) -> list[MemoryRetrievalResult]:
        """执行混合检索（默认跨会话全库）.

        长期记忆定位为跨会话召回：不按 session_id 过滤，任何会话沉淀的
        记忆都可被召回；filter_params 为可选显式过滤（按需传入 category/
        type 等），产线不传即全库检索。
        """
        started = time.perf_counter()
        # 并发启动原始向量和关键词路线；扩展 query 只作为完成原始路线后的
        # 附加向量证据。
        keyword_task = asyncio.create_task(
            self._keyword_search(query, filter_params, route="keyword")
        )
        raw_vector_task = asyncio.create_task(
            self._vector_search(query, filter_params, route="vector")
        )
        expansion_started = time.perf_counter()
        expanded_query = await self._expand_query(query) if expand_query else None
        logger.info(
            "memory_retrieval_query_expansion_completed",
            duration_ms=round((time.perf_counter() - expansion_started) * 1000),
            enabled=expand_query,
        )

        keyword_results = await keyword_task
        vector_results = await raw_vector_task
        if expanded_query and expanded_query.strip() != query.strip():
            rewrite_started = time.perf_counter()
            vector_results.extend(
                await self._vector_search(
                    expanded_query,
                    filter_params,
                    route="vector_rewrite",
                )
            )
            logger.info(
                "memory_retrieval_query_rewrite_completed",
                duration_ms=round((time.perf_counter() - rewrite_started) * 1000),
            )

        logger.info(
            "memory_retrieval_completed",
            duration_ms=round((time.perf_counter() - started) * 1000),
            keyword_count=len(keyword_results),
            vector_count=len(vector_results),
            expanded=bool(expanded_query),
        )

        fused = self._reciprocal_rank_fusion(
            vector_results,
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
        selected = fused[: self._rerank_k][: self._context_k]
        return selected

    async def _expand_query(self, query: str) -> str | None:
        """使用 LLM 扩展查询.

        扩展结果仅用于向量语义检索（关键词检索使用原始查询走 FTS5），
        因此提示词明确语义检索目标、约束输出为单行语句。
        """
        prompt = get_prompt("query_expansion").format(query=query)
        try:
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            expanded = extract_message_text(response).strip()
            if not expanded:
                # 空响应（模型返回工具调用/错误/无文本）静默回落为 "None" 曾导致
                # 下游检索被垃圾查询污染且无日志，这里显式记录并回退原查询。
                logger.warning("query_expand_empty", query_length=len(query))
                return None
            return expanded
        except Exception as e:
            logger.warning("query_expand_failed", error=str(e))
            return None

    async def _vector_search(
        self,
        query: str,
        filter_params: dict[str, Any] | None,
        record_access: bool = False,
        *,
        route: str = "vector",
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
        has_raw_native_scores = any(r.get("native_score") is not None for r in results)
        for i, r in enumerate(results, 1):
            out.append(
                MemoryRetrievalResult(
                    content=r["content"],
                    source=route,
                    metadata=r.get("metadata", {}),
                    native_score=r.get("native_score", r.get("score")),
                    rank_sources={route: i},
                    memory_id=r.get("id", str(i)),
                )
            )
        # RRF 依赖列表位置作为 rank（位置越靠前贡献越大），必须"最相关在前"。
        # score 为原始相似度 1-dist/2（越大越相似），vector_weight 统一在 RRF
        # 融合处生效，这里不乘权重，避免融合改读 r.native_score 时双倍加权。上游
        # ChromaDB 已按距离升序返回，这里显式按 score 降序把不变量固化。
        out.sort(
            key=lambda x: x.native_score or 0.0,
            reverse=not has_raw_native_scores,
        )
        out = [
            result
            for result in out
            if (result.native_score or 0.0) >= self._vector_min_score
        ]
        return out

    async def _keyword_search(
        self,
        query: str,
        filter_params: dict[str, Any] | None,
        record_access: bool = False,
        *,
        route: str = "keyword",
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
                    source=route,
                    metadata=r.get("metadata", {}),
                    native_score=r.get("score"),
                    exact_match=bool(r.get("exact_match"))
                    or self._is_exact_match(query, r["content"]),
                    rank_sources={route: i},
                    memory_id=r.get("id", str(i)),
                )
            )
        # 同上：RRF 需要"最相关在前"。score 为原始相似度 |bm25|/(1+|bm25|)
        # （越大越相关），keyword_weight 统一在 RRF 融合处生效。上游已
        # ORDER BY rank(=bm25 升序) 返回，这里显式按 score 降序固化不变量。
        out.sort(key=lambda x: x.native_score or 0.0, reverse=True)
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
        for rank, r in enumerate(vector_results, 1):
            route = r.source if r.source.startswith("vector") else "vector"
            route_rank = r.rank_sources.get(route, rank)
            if r.memory_id not in scores:
                scores[r.memory_id] = 0.0
                content_map[r.memory_id] = r
            content_map[r.memory_id].rank_sources.setdefault(route, route_rank)
            scores[r.memory_id] += self._vector_weight / (self._rrf_k + route_rank)

        for rank, r in enumerate(keyword_results, 1):
            route = "keyword"
            route_rank = r.rank_sources.get(route, rank)
            if r.memory_id not in scores:
                scores[r.memory_id] = 0.0
                content_map[r.memory_id] = r
            content_map[r.memory_id].rank_sources.setdefault(route, route_rank)
            content_map[r.memory_id].exact_match = (
                content_map[r.memory_id].exact_match or r.exact_match
            )
            scores[r.memory_id] += self._keyword_weight / (self._rrf_k + route_rank)

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

    async def get_relevant_memories(
        self,
        user_message: str,
    ) -> str:
        """获取相关记忆并格式化为系统提示（跨会话召回）."""
        try:
            results = await self._manager.retrieve(
                query=user_message,
            )
        except Exception as e:
            logger.warning("memory_retrieve_failed", error=str(e))
            return ""

        if not results:
            return ""

        parts = ["[相关记忆]"]
        total_tokens = 0
        selected_ids: list[str] = []
        for r in results:
            content_tokens = self._token_counter.count_text_tokens(r.content)
            if total_tokens + content_tokens > self._max_tokens:
                break
            parts.append(f"- {r.content}")
            total_tokens += content_tokens
            selected_ids.append(r.memory_id)

        if len(parts) > 1:
            parts.append("[/相关记忆]")
            self._manager.record_selected_access(selected_ids)
            return "\n".join(parts)
        return ""

    async def get_context(self, request: MemoryRetrievalRequest) -> str:
        """Retrieve and assemble context from an explicit runtime request."""
        try:
            results = await self._manager.retrieve(
                query=request.query,
                expand_query=request.expand_query,
            )
        except Exception as e:
            logger.warning("memory_retrieve_failed", error=str(e))
            return ""
        return self._format_results(results[: request.limit])

    def _format_results(self, results: list[MemoryRetrievalResult]) -> str:
        if not results:
            return ""
        parts = ["[相关记忆]"]
        total_tokens = 0
        selected_ids: list[str] = []
        for result in results:
            content_tokens = self._token_counter.count_text_tokens(result.content)
            if total_tokens + content_tokens > self._max_tokens:
                break
            parts.append(f"- {result.content}")
            total_tokens += content_tokens
            selected_ids.append(result.memory_id)
        if len(parts) == 1:
            return ""
        parts.append("[/相关记忆]")
        self._manager.record_selected_access(selected_ids)
        return "\n".join(parts)
