"""混合检索管理器 — LLM 查询扩展 + 向量检索 + 关键词检索 + RRF 融合 + 时间衰减.

技术方案：LLM 提取 + 向量数据库 + 混合检索。
"""

from __future__ import annotations

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
        keyword_results = await self._keyword_search(
            query, session_id, filter_params
        )

        # 4. RRF 融合
        fused = self._reciprocal_rank_fusion(vector_results, keyword_results)

        # 5. 时间衰减 + 过滤 + 排序
        for r in fused:
            r.score *= self._calculate_temporal_decay(r.metadata)

        filtered = [r for r in fused if r.score >= self._min_score * 0.1]
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
        try:
            results = await self._memory.search(
                query=query,
                session_id=session_id,
                n_results=self._top_k * 2,
                where=filter_params,
            )
        except Exception as e:
            logger.warning("vector_search_failed", error=str(e))
            return []

        out: list[SearchResult] = []
        for i, r in enumerate(results, 1):
            out.append(SearchResult(
                content=r["content"],
                score=r["score"] * self._vector_weight,
                source="vector",
                metadata=r.get("metadata", {}),
                chunk_id=r.get("id", str(i)),
            ))
        return out

    async def _keyword_search(
        self,
        query: str,
        session_id: str,
        filter_params: dict[str, Any] | None,
    ) -> list[SearchResult]:
        """关键词检索（SQLite FTS5 MATCH 全文检索）.

        通过 MemoryManager.keyword_search() 走 FTS5 虚拟表的 MATCH 操作符，
        tokenize='unicode61' 支持中文分词，bm25 算法排序。
        """
        try:
            results = await self._memory.keyword_search(
                query=query,
                session_id=session_id,
                n_results=self._top_k * 2,
            )
        except Exception as e:
            logger.warning("keyword_search_failed", error=str(e))
            return []

        out: list[SearchResult] = []
        for i, r in enumerate(results, 1):
            out.append(SearchResult(
                content=r["content"],
                score=r["score"] * self._keyword_weight,
                source="keyword",
                metadata=r.get("metadata", {}),
                chunk_id=r.get("id", str(i)),
            ))
        return out

    def _reciprocal_rank_fusion(
        self,
        vector_results: list[SearchResult],
        keyword_results: list[SearchResult],
    ) -> list[SearchResult]:
        """倒数排名融合 (RRF)."""
        scores: dict[str, float] = {}
        content_map: dict[str, SearchResult] = {}

        for rank, r in enumerate(vector_results, 1):
            if r.chunk_id not in scores:
                scores[r.chunk_id] = 0.0
                content_map[r.chunk_id] = r
            scores[r.chunk_id] += 1.0 / (self._rrf_k + rank)

        for rank, r in enumerate(keyword_results, 1):
            if r.chunk_id not in scores:
                scores[r.chunk_id] = 0.0
                content_map[r.chunk_id] = r
            scores[r.chunk_id] += 1.0 / (self._rrf_k + rank)

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
