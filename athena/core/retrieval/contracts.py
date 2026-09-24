"""检索轨迹写入所需的领域值对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RetrievalRunRequest:
    """描述一次检索运行及其可复现配置。"""

    query: str
    scope: str
    config: dict[str, Any] = field(default_factory=dict)
    index_generation: str | None = None
    session_id: str | None = None
    agent_run_id: str | None = None
    message_id: str | None = None


@dataclass(frozen=True)
class RetrievalCandidate:
    """描述检索流水线某一阶段看到的一个候选。"""

    provider: str
    stage: str
    source_type: str
    source_id: str
    native_rank: int | None = None
    native_score: float | None = None
    fused_rank: int | None = None
    fused_score: float | None = None
    rerank_rank: int | None = None
    rerank_score: float | None = None
    logical_source_id: str | None = None
    revision_id: str | None = None
    document_version_id: str | None = None
    filter_reason: str | None = None
    selected_for_result: bool = False
    injected_into_context: bool = False
    content_preview: str | None = None
    source_title: str | None = None
    locator: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
