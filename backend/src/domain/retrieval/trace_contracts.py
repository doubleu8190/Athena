"""召回分析记录使用的领域类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class RetrievalHitTrace:
    """一个检索通道对候选的命中信息。"""

    channel: str
    rank: int
    score: float | None = None

    def __post_init__(self) -> None:
        """校验通道名、排名和可选分数。"""
        if not self.channel.strip():
            raise ValueError("retrieval_trace_channel_required")
        if type(self.rank) is not int or self.rank < 1:
            raise ValueError("retrieval_trace_rank_invalid")


@dataclass(frozen=True, slots=True)
class RetrievalChannelTrace:
    """一个检索通道的执行结果。"""

    channel: str
    status: Literal["succeeded", "failed", "timeout", "skipped"]
    hit_count: int
    error_message: str | None = None

    def __post_init__(self) -> None:
        """校验通道身份及命中数量。"""
        if not self.channel.strip():
            raise ValueError("retrieval_trace_channel_required")
        if type(self.hit_count) is not int or self.hit_count < 0:
            raise ValueError("retrieval_trace_hit_count_invalid")


@dataclass(frozen=True, slots=True)
class RetrievalCandidateTrace:
    """一个来源在某个检索模块中的完整处理信息。"""

    provider: str
    source_type: str
    source_id: str
    logical_source_id: str | None = None
    revision_id: str | None = None
    native_rank: int | None = None
    native_score: float | None = None
    fused_rank: int | None = None
    fused_score: float | None = None
    rerank_rank: int | None = None
    rerank_score: float | None = None
    selected_for_result: bool = False
    filter_reason: str | None = None
    content_preview: str | None = None
    source_title: str | None = None
    locator: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)
    hits: tuple[RetrievalHitTrace, ...] = ()
    context_selected: bool | None = None
    selection_stage: Literal["provider", "bundle", "window"] | None = None

    def __post_init__(self) -> None:
        """校验候选来源身份和各阶段排名。"""
        if any(not value.strip() for value in (self.provider, self.source_type, self.source_id)):
            raise ValueError("retrieval_trace_source_identity_required")
        for rank in (self.native_rank, self.fused_rank, self.rerank_rank):
            if rank is not None and (type(rank) is not int or rank < 1):
                raise ValueError("retrieval_trace_rank_invalid")
        if not isinstance(self.locator, dict) or not isinstance(self.metadata, dict):
            raise ValueError("retrieval_trace_metadata_invalid")
        if any(not isinstance(hit, RetrievalHitTrace) for hit in self.hits):
            raise ValueError("retrieval_trace_hits_invalid")


@dataclass(frozen=True, slots=True)
class RetrievalProviderTrace:
    """一次检索模块调用的摘要。"""

    provider: str
    status: Literal["succeeded", "partial", "failed", "timeout", "skipped", "cancelled"]
    duration_ms: float
    candidate_count: int
    selected_count: int
    query: str | None = None
    resource_scope: dict[str, object] = field(default_factory=dict)
    effective_params: dict[str, object] = field(default_factory=dict)
    error_message: str | None = None
    channels: tuple[RetrievalChannelTrace, ...] = ()

    def __post_init__(self) -> None:
        """校验提供方、耗时、计数和嵌套通道结果。"""
        if not self.provider.strip():
            raise ValueError("retrieval_trace_provider_required")
        if self.duration_ms < 0:
            raise ValueError("retrieval_trace_duration_invalid")
        if self.candidate_count < 0 or self.selected_count < 0:
            raise ValueError("retrieval_trace_count_invalid")
        if not isinstance(self.resource_scope, dict) or not isinstance(self.effective_params, dict):
            raise ValueError("retrieval_trace_params_invalid")
        if any(not isinstance(channel, RetrievalChannelTrace) for channel in self.channels):
            raise ValueError("retrieval_trace_channels_invalid")


@dataclass(frozen=True, slots=True)
class RetrievalCandidateSelection:
    """Root 合并资料窗口后，对本次召回候选作出的选择。"""

    provider: str
    source_id: str
    context_selected: bool
    source_ids: tuple[str, ...] = ()
    filter_reason: str | None = None

    def __post_init__(self) -> None:
        """校验选择对应的提供方和来源。"""
        if not self.provider.strip() or not self.source_id.strip():
            raise ValueError("retrieval_selection_identity_required")


@dataclass(frozen=True, slots=True)
class RetrievalRunTrace:
    """一次检索运行的领域记录。"""

    raw_hit_count: int
    providers: tuple[RetrievalProviderTrace, ...] = ()
    selection_status: Literal["pending", "complete"] = "pending"
    context_count: int | None = None
    trace_version: Literal[2] = 2

    def __post_init__(self) -> None:
        """校验当前记录版本及命中数量。"""
        if self.trace_version != 2:
            raise ValueError("retrieval_trace_version_unsupported")
        if self.raw_hit_count < 0:
            raise ValueError("retrieval_trace_raw_hit_count_invalid")
        if self.context_count is not None and self.context_count < 0:
            raise ValueError("retrieval_trace_context_count_invalid")
        if any(not isinstance(provider, RetrievalProviderTrace) for provider in self.providers):
            raise ValueError("retrieval_trace_providers_invalid")


__all__ = [
    "RetrievalCandidateSelection",
    "RetrievalCandidateTrace",
    "RetrievalChannelTrace",
    "RetrievalHitTrace",
    "RetrievalProviderTrace",
    "RetrievalRunTrace",
]
