"""文件检索流水线使用的领域候选模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class FileRetrievalCandidate:
    """文件检索各阶段共享的强类型候选。"""

    source_id: str
    content: str
    attachment_id: str
    locator: dict[str, Any] = field(default_factory=dict)
    source_title: str | None = None
    document_version: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    keyword_score: float | None = None
    vector_score: float | None = None
    fused_score: float | None = None
    rerank_score: float | None = None
    keyword_rank: int | None = None
    vector_rank: int | None = None
    fused_rank: int | None = None
    rerank_rank: int | None = None
    filter_reason: str | None = None
    selected_for_result: bool = False
    retrieval_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """将内部候选序列化为现有文件搜索 API 的字典格式。"""
        payload = asdict(self)
        payload["id"] = payload.pop("source_id")
        payload["score"] = next(
            (
                value
                for value in (
                    self.rerank_score,
                    self.fused_score,
                    self.keyword_score,
                    self.vector_score,
                )
                if value is not None
            ),
            None,
        )
        return payload


__all__ = ["FileRetrievalCandidate"]
