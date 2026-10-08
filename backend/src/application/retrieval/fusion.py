"""迁移期检索融合 facade；规则本体位于 domain.retrieval。"""

from collections.abc import Sequence

from domain.retrieval import RetrievalCandidate
from domain.retrieval.fusion import RetrievalRankFusion


class RetrievalFusionService(RetrievalRankFusion):
    """Application-facing name for the domain ranking rule."""

    def fuse(
        self,
        keyword: Sequence[RetrievalCandidate],
        vector: Sequence[RetrievalCandidate],
        *,
        limit: int = 10,
    ) -> list[RetrievalCandidate]:
        """兼容旧调用方；新代码使用 ``fuse_ranked_candidates``。"""
        return self.fuse_ranked_candidates(keyword, vector, limit=limit)


HybridRetrievalService = RetrievalFusionService

__all__ = ["HybridRetrievalService", "RetrievalFusionService"]
