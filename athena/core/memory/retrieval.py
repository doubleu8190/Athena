"""混合检索管理器 — LLM 查询扩展 + 向量检索 + 关键词检索 + RRF 融合 + 时间衰减.

技术方案：LLM 提取 + 向量数据库 + 混合检索。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage

from athena.config.settings import Settings, get_settings
from athena.core.llm.provider import LLMProvider
from athena.core.memory.memory import MemoryManager
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


# 中英文停用词
_STOP_WORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "of", "in", "on", "at",
    "to", "for", "with", "by", "and", "or", "not", "no",
    "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都",
    "这", "一个", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这",
}


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
        """使用 LLM 扩展查询."""
        prompt = (
            "请将以下用户查询扩展为更完整的搜索语句，提取关键实体和意图，"
            "直接返回扩展后的查询（不要解释）：\n\n原查询: " + query
        )
        try:
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            content = getattr(response, "content", str(response))
            return content.strip() if isinstance(content, str) else str(content).strip()
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
        """关键词检索（基于 ChromaDB 元数据中的所有文档进行精确匹配）."""
        keywords = self._extract_keywords(query)
        if not keywords:
            return []

        try:
            all_data = await self._memory.search(
                query=query,
                session_id=session_id,
                n_results=50,  # 拉取较多候选用于关键词匹配
                where=filter_params,
            )
        except Exception as e:
            logger.warning("keyword_search_failed", error=str(e))
            return []

        results: list[SearchResult] = []
        for i, r in enumerate(all_data, 1):
            score = self._calculate_keyword_score(r["content"], keywords)
            if score > 0:
                results.append(SearchResult(
                    content=r["content"],
                    score=score * self._keyword_weight,
                    source="keyword",
                    metadata=r.get("metadata", {}),
                    chunk_id=r.get("id", str(i)),
                ))
        results.sort(key=lambda x: x.score, reverse=True)
        return results[: self._top_k]

    def _extract_keywords(self, query: str) -> list[str]:
        """从查询中提取关键词（中英文）。"""
        # 英文/数字 token
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]+", query.lower())
        # 中文 token（按字符切分简单处理，后续可换 jieba）
        cn_chars = re.findall(r"[\u4e00-\u9fa5]+", query)
        tokens.extend(cn_chars)
        return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]

    def _calculate_keyword_score(self, document: str, keywords: list[str]) -> float:
        """计算关键词匹配分数."""
        doc_lower = document.lower()
        score = 0.0
        for kw in keywords:
            if kw in doc_lower:
                score += 0.5
                if re.search(r"\b" + re.escape(kw) + r"\b", doc_lower):
                    score += 0.3
                freq = doc_lower.count(kw)
                score += min(freq * 0.1, 0.2)
        return min(score, 1.0)

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
        max_tokens: int | None = None,
    ) -> str:
        """获取相关记忆并格式化为系统提示."""
        max_tokens = max_tokens or get_settings().memory_max_tokens
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
