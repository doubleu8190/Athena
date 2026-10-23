"""召回分析记录使用的固定 JSON 结构。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RetrievalHitTrace(BaseModel):
    """单个检索通道对候选的命中信息。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: str = Field(min_length=1)
    rank: int = Field(ge=1)
    score: float | None = None


class RetrievalChannelTrace(BaseModel):
    """一个检索通道的执行结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: str = Field(min_length=1)
    status: Literal["succeeded", "failed", "timeout", "skipped"]
    hit_count: int = Field(ge=0)
    error_message: str | None = None


class RetrievalCandidateTrace(BaseModel):
    """一个来源在某个检索模块中的完整处理信息。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    logical_source_id: str | None = None
    revision_id: str | None = None
    native_rank: int | None = Field(default=None, ge=1)
    native_score: float | None = None
    fused_rank: int | None = Field(default=None, ge=1)
    fused_score: float | None = None
    rerank_rank: int | None = Field(default=None, ge=1)
    rerank_score: float | None = None
    selected_for_result: bool = False
    filter_reason: str | None = None
    content_preview: str | None = None
    source_title: str | None = None
    locator: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)
    hits: tuple[RetrievalHitTrace, ...] = ()
    context_selected: bool | None = None
    selection_stage: Literal["provider", "bundle", "window"] | None = None


class RetrievalProviderTrace(BaseModel):
    """一次检索模块调用的摘要。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    query: str | None = None
    resource_scope: dict[str, object] = Field(default_factory=dict)
    effective_params: dict[str, object] = Field(default_factory=dict)
    status: Literal["succeeded", "partial", "failed", "timeout", "skipped", "cancelled"]
    duration_ms: float = Field(ge=0)
    error_message: str | None = None
    channels: tuple[RetrievalChannelTrace, ...] = ()
    candidate_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)


class RetrievalCandidateSelection(BaseModel):
    """Root 合并资料窗口后，对本次召回候选作出的选择。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    context_selected: bool
    filter_reason: str | None = None


class RetrievalRunTrace(BaseModel):
    """写入 retrieval_runs.config_json 的本版记录。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_version: Literal[2] = 2
    raw_hit_count: int = Field(ge=0)
    providers: tuple[RetrievalProviderTrace, ...] = ()
    selection_status: Literal["pending", "complete"] = "pending"
    context_count: int | None = Field(default=None, ge=0)


__all__ = [
    "RetrievalCandidateSelection",
    "RetrievalCandidateTrace",
    "RetrievalChannelTrace",
    "RetrievalHitTrace",
    "RetrievalProviderTrace",
    "RetrievalRunTrace",
]
