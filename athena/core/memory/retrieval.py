"""混合检索管理器 — LLM 查询扩展 + 向量检索 + 关键词检索 + RRF 融合 + 时间衰减.

技术方案：LLM 提取 + 向量数据库 + 混合检索。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage

from athena.config.settings import Settings, get_settings
from athena.core.llm.provider import LLMProvider
from athena.core.memory.memory import MemoryManager
from athena.utils.llm import extract_message_text
from athena.utils.logging import get_logger

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
    source: str  # "vector" / "keyword" / "fused"
    metadata: dict[str, Any]
    chunk_id: str


class HybridRetrievalManager:
    """混合检索管理器.

    流程：LLM 查询扩展 → 向量检索 + 关键词检索 → RRF 融合 → 时间衰减 → 过滤排序.
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        memory_manager: MemoryManager,
        settings: Settings | None = None,
    ) -> None:
        self._llm = llm_provider
        self._memory = memory_manager
        self._settings = settings or get_settings()
        self._min_score = self._settings.memory_min_score
        self._top_k = self._settings.retrieval_top_k
        self._vector_weight = self._settings.vector_weight
        self._keyword_weight = self._settings.keyword_weight
        self._rrf_k = self._settings.rrf_k
        self._ttl_days = self._settings.memory_ttl_days

    async def retrieve(
        self,
        query: str,
        session_id: str,
        filter_params: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """执行混合检索."""
        # 1. LLM 查询扩展
        expanded_query = await self._expand_query(query)

        # 2. 向量检索
        vector_results = await self._vector_search(
            expanded_query or query, session_id, filter_params
        )

        # 3. 关键词检索
        keyword_results = await self._keyword_search(query, session_id, filter_params)

        # 4. RRF 融合
        fused = self._reciprocal_rank_fusion(vector_results, keyword_results)

        # 5. 记忆度叠加 + 时间衰减 + 过滤 + 排序
        # 未落盘的访问统计先叠加进 metadata，使加权实时生效（不等同步周期）
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

        # RRF 分数上界约 2/(rrf_k+1)；按比例放缩阈值，避免永远为空
        # 1. 阈值重标定 —— 把 0-1 语义阈值映射到 RRF 分数尺度
        # memory_min_score=0.7 是原始语义相似度上的阈值（向量 score 1-dist/2 的范围）。但融合后 r.score 已经是 RRF 分数，它的上界极小：
        # 满分（两个列表都排第 1）：0.75/61 + 0.25/61 = 1/61 ≈ 0.0164
        # 如果不缩放，score >= 0.7 永远不成立 → retrieve() 永远返回空数组。所以除以 (rrf_k+1) 把 0.7 重新映射到 RRF 的尺度上：0.7/61 ≈ 0.0115。
        # 这个值的具体含义：约等于"向量路排前 5"的命中（0.75/61 ≈ 0.0123 ≥ 0.0115 通过，rank 6 起 0.75/66 ≈ 0.0114 开始不过），正好对齐 retrieval_top_k=5。
        # 2. 相关性下限过滤 —— 记忆的"准入门槛"
        # 只保留融合证据足够强的记忆，防止噪声/弱命中污染 agent 上下文。注意它作用的是已经乘过时间衰减和记忆度的分（:95-96），所以：
        # 记忆度高（频繁+近期被召回，≥1.0 加成）→ 更容易过线
        # 记忆久远（衰减到 0.1）→ 更难过线
        # 一个有意思的副作用：纯关键词命中的结果永远过不了线（关键词单路最高 0.25/61 ≈ 0.0041 < 0.0115）。这是符合职责设计的——关键词路只是"精确词/前缀"的弱助力，用来给双路命中叠加贡献，而不是独立召回通道；真正的语义召回由向量路承担。
        threshold = self._min_score / (self._rrf_k + 1)
        filtered = [r for r in fused if r.score >= threshold]
        filtered.sort(key=lambda x: x.score, reverse=True)
        return filtered[: self._top_k]

    async def _expand_query(self, query: str) -> str:
        """使用 LLM 扩展查询.

        扩展结果仅用于向量语义检索（关键词检索使用原始查询走 FTS5），
        因此提示词明确语义检索目标、约束输出为单行语句。
        """
        prompt = (
            "你是记忆检索查询优化器。请将用户查询改写为适合向量语义检索的"
            "搜索语句，用于从用户的历史记忆中召回相关内容。\n"
            "要求：\n"
            "- 突出核心实体与意图（人名、项目名、技术名词、日期等）\n"
            "- 补充近义词或同义改写以扩大语义召回\n"
            "- 保持与原始查询相同的语言\n"
            "- 只返回一行搜索语句，不要任何解释、前缀、引号或编号\n\n"
            f"原始查询：{query}"
        )
        try:
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            expanded = extract_message_text(response).strip()
            if not expanded:
                # 空响应（模型返回工具调用/错误/无文本）静默回落为 "None" 曾导致
                # 下游检索被垃圾查询污染且无日志，这里显式记录并回退原查询。
                logger.warning("query_expand_empty", query=query[:50])
                return query
            return expanded
        except Exception as e:
            logger.warning("query_expand_failed", error=str(e))
            return query

    async def _vector_search(
        self,
        query: str,
        session_id: str,
        filter_params: dict[str, Any] | None,
    ) -> list[SearchResult]:
        """向量检索."""
        where = {"session_id": session_id}
        if filter_params:
            where.update(filter_params)
        try:
            results = await self._memory.search(
                query=query,
                n_results=self._top_k * 2,
                where=where,
            )
        except Exception as e:
            logger.warning("vector_search_failed", error=str(e))
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
        return out

    async def _keyword_search(
        self,
        query: str,
        session_id: str,
        filter_params: dict[str, Any] | None,
    ) -> list[SearchResult]:
        """关键词检索（SQLite FTS5 MATCH 全文检索）.

        通过 MemoryManager.keyword_search() 走 FTS5 虚拟表的 MATCH 操作符，
        tokenize='unicode61' 仅做精确词/整段/前缀召回，不做中文语义分词
        （分词语义由向量检索承担），bm25 算法排序。
        """
        try:
            results = await self._memory.keyword_search(
                query=query,
                n_results=self._top_k * 2,
                where={"session_id": session_id},
            )
        except Exception as e:
            logger.warning("keyword_search_failed", error=str(e))
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
        return out

    def _reciprocal_rank_fusion(
        self,
        vector_results: list[SearchResult],
        keyword_results: list[SearchResult],
    ) -> list[SearchResult]:
        """倒数排名融合 (RRF). RRF 的核心思想是：一个文档在多个检索结果列表中的排名越靠前（即排名数字越小），其融合分数越高。"""
        scores: dict[str, float] = {}
        content_map: dict[str, SearchResult] = {}

        # 加权 RRF：按来源权重缩放贡献，使 vector_weight / keyword_weight 真正生效
        #（职责边界：向量路承担语义，关键词路仅作精确词/前缀助力的弱贡献）
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

    def __init__(self, retrieval_manager: HybridRetrievalManager) -> None:
        self._manager = retrieval_manager

    async def get_relevant_memories(
        self,
        user_message: str,
        session_id: str,
    ) -> str:
        """获取相关记忆并格式化为系统提示."""
        max_tokens = get_settings().memory_max_tokens
        try:
            results = await self._manager.retrieve(
                query=user_message,
                session_id=session_id,
            )
        except Exception as e:
            logger.warning("memory_retrieve_failed", error=str(e))
            return ""

        if not results:
            return ""

        parts = ["[相关记忆]"]
        total_tokens = 0
        for r in results:
            content_tokens = len(r.content) // 4
            if total_tokens + content_tokens > max_tokens:
                break
            parts.append(f"- {r.content}")
            total_tokens += content_tokens

        if len(parts) > 1:
            parts.append("[/相关记忆]")
            return "\n".join(parts)
        return ""
