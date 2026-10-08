"""应用层检索融合服务。"""
from domain.retrieval.fusion import RetrievalRankFusion


class RetrievalFusionService(RetrievalRankFusion):
    """Application-facing name for the domain ranking rule."""

__all__ = ["RetrievalFusionService"]
