"""检索跟踪 JSON schema 及其到领域类型的转换。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from domain.retrieval.trace_contracts import (
    RetrievalCandidateSelection,
    RetrievalCandidateTrace,
    RetrievalChannelTrace,
    RetrievalHitTrace,
    RetrievalProviderTrace,
    RetrievalRunTrace,
)


class RetrievalHitTraceSchema(BaseModel):
    """检索命中在 JSON 边界上的校验结构。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: str = Field(min_length=1)
    rank: int = Field(ge=1)
    score: float | None = None

    def to_domain(self) -> RetrievalHitTrace:
        """将检索追踪 schema 转换为对应的领域对象。

        参数：
        - 无参数。

        返回：
        - RetrievalHitTrace：记录单条检索命中的通道、名次和分数。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return RetrievalHitTrace(channel=self.channel, rank=self.rank, score=self.score)

    @classmethod
    def from_domain(cls, value: RetrievalHitTrace) -> RetrievalHitTraceSchema:
        """将检索追踪领域对象转换为对应的 schema。

        参数：
        - `value`（RetrievalHitTrace）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - RetrievalHitTraceSchema：记录单条检索命中的通道、名次和分数。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return cls(channel=value.channel, rank=value.rank, score=value.score)


class RetrievalChannelTraceSchema(BaseModel):
    """检索通道结果在 JSON 边界上的校验结构。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: str = Field(min_length=1)
    status: Literal["succeeded", "failed", "timeout", "skipped"]
    hit_count: int = Field(ge=0)
    error_message: str | None = None

    def to_domain(self) -> RetrievalChannelTrace:
        """将检索追踪 schema 转换为对应的领域对象。

        参数：
        - 无参数。

        返回：
        - RetrievalChannelTrace：记录检索通道状态、命中数和可选错误信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return RetrievalChannelTrace(
            channel=self.channel,
            status=self.status,
            hit_count=self.hit_count,
            error_message=self.error_message,
        )

    @classmethod
    def from_domain(cls, value: RetrievalChannelTrace) -> RetrievalChannelTraceSchema:
        """将检索追踪领域对象转换为对应的 schema。

        参数：
        - `value`（RetrievalChannelTrace）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - RetrievalChannelTraceSchema：记录检索通道状态、命中数和可选错误信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return cls(
            channel=value.channel,
            status=value.status,
            hit_count=value.hit_count,
            error_message=value.error_message,
        )


class RetrievalCandidateTraceSchema(BaseModel):
    """候选处理详情在 JSON 边界上的校验结构。"""

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
    hits: tuple[RetrievalHitTraceSchema, ...] = ()
    context_selected: bool | None = None
    selection_stage: Literal["provider", "bundle", "window"] | None = None

    def to_domain(self) -> RetrievalCandidateTrace:
        """将检索追踪 schema 转换为对应的领域对象。

        参数：
        - 无参数。

        返回：
        - RetrievalCandidateTrace：统一格式的检索候选内容及其来源信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return RetrievalCandidateTrace(
            provider=self.provider,
            source_type=self.source_type,
            source_id=self.source_id,
            logical_source_id=self.logical_source_id,
            revision_id=self.revision_id,
            native_rank=self.native_rank,
            native_score=self.native_score,
            fused_rank=self.fused_rank,
            fused_score=self.fused_score,
            rerank_rank=self.rerank_rank,
            rerank_score=self.rerank_score,
            selected_for_result=self.selected_for_result,
            filter_reason=self.filter_reason,
            content_preview=self.content_preview,
            source_title=self.source_title,
            locator=dict(self.locator),
            metadata=dict(self.metadata),
            hits=tuple(hit.to_domain() for hit in self.hits),
            context_selected=self.context_selected,
            selection_stage=self.selection_stage,
        )

    @classmethod
    def from_domain(cls, value: RetrievalCandidateTrace) -> RetrievalCandidateTraceSchema:
        """将检索追踪领域对象转换为对应的 schema。

        参数：
        - `value`（RetrievalCandidateTrace）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - RetrievalCandidateTraceSchema：统一格式的检索候选内容及其来源信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return cls(
            provider=value.provider,
            source_type=value.source_type,
            source_id=value.source_id,
            logical_source_id=value.logical_source_id,
            revision_id=value.revision_id,
            native_rank=value.native_rank,
            native_score=value.native_score,
            fused_rank=value.fused_rank,
            fused_score=value.fused_score,
            rerank_rank=value.rerank_rank,
            rerank_score=value.rerank_score,
            selected_for_result=value.selected_for_result,
            filter_reason=value.filter_reason,
            content_preview=value.content_preview,
            source_title=value.source_title,
            locator=value.locator,
            metadata=value.metadata,
            hits=tuple(RetrievalHitTraceSchema.from_domain(hit) for hit in value.hits),
            context_selected=value.context_selected,
            selection_stage=value.selection_stage,
        )


class RetrievalProviderTraceSchema(BaseModel):
    """检索调用摘要在 JSON 边界上的校验结构。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    query: str | None = None
    resource_scope: dict[str, object] = Field(default_factory=dict)
    effective_params: dict[str, object] = Field(default_factory=dict)
    status: Literal["succeeded", "partial", "failed", "timeout", "skipped", "cancelled"]
    duration_ms: float = Field(ge=0)
    error_message: str | None = None
    channels: tuple[RetrievalChannelTraceSchema, ...] = ()
    candidate_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)

    def to_domain(self) -> RetrievalProviderTrace:
        """将检索追踪 schema 转换为对应的领域对象。

        参数：
        - 无参数。

        返回：
        - RetrievalProviderTrace：记录 Provider 查询、耗时、候选数和通道追踪。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return RetrievalProviderTrace(
            provider=self.provider,
            status=self.status,
            duration_ms=self.duration_ms,
            candidate_count=self.candidate_count,
            selected_count=self.selected_count,
            query=self.query,
            resource_scope=dict(self.resource_scope),
            effective_params=dict(self.effective_params),
            error_message=self.error_message,
            channels=tuple(channel.to_domain() for channel in self.channels),
        )

    @classmethod
    def from_domain(cls, value: RetrievalProviderTrace) -> RetrievalProviderTraceSchema:
        """将检索追踪领域对象转换为对应的 schema。

        参数：
        - `value`（RetrievalProviderTrace）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - RetrievalProviderTraceSchema：记录 Provider 查询、耗时、候选数和通道追踪。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return cls(
            provider=value.provider,
            query=value.query,
            resource_scope=value.resource_scope,
            effective_params=value.effective_params,
            status=value.status,
            duration_ms=value.duration_ms,
            error_message=value.error_message,
            channels=tuple(
                RetrievalChannelTraceSchema.from_domain(channel)
                for channel in value.channels
            ),
            candidate_count=value.candidate_count,
            selected_count=value.selected_count,
        )


class RetrievalCandidateSelectionSchema(BaseModel):
    """Root 候选选择在 JSON 边界上的校验结构。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_ids: tuple[str, ...] = ()
    context_selected: bool
    filter_reason: str | None = None

    def to_domain(self) -> RetrievalCandidateSelection:
        """将检索追踪 schema 转换为对应的领域对象。

        参数：
        - 无参数。

        返回：
        - RetrievalCandidateSelection：统一格式的检索候选内容及其来源信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return RetrievalCandidateSelection(
            provider=self.provider,
            source_id=self.source_id,
            source_ids=self.source_ids,
            context_selected=self.context_selected,
            filter_reason=self.filter_reason,
        )

    @classmethod
    def from_domain(cls, value: RetrievalCandidateSelection) -> RetrievalCandidateSelectionSchema:
        """将检索追踪领域对象转换为对应的 schema。

        参数：
        - `value`（RetrievalCandidateSelection）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - RetrievalCandidateSelectionSchema：统一格式的检索候选内容及其来源信息。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return cls(
            provider=value.provider,
            source_id=value.source_id,
            source_ids=value.source_ids,
            context_selected=value.context_selected,
            filter_reason=value.filter_reason,
        )


class RetrievalRunTraceSchema(BaseModel):
    """检索运行记录在 JSON 边界上的校验结构。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_version: Literal[2] = 2
    raw_hit_count: int = Field(ge=0)
    providers: tuple[RetrievalProviderTraceSchema, ...] = ()
    selection_status: Literal["pending", "complete"] = "pending"
    context_count: int | None = Field(default=None, ge=0)

    def to_domain(self) -> RetrievalRunTrace:
        """将检索追踪 schema 转换为对应的领域对象。

        参数：
        - 无参数。

        返回：
        - RetrievalRunTrace：记录一次检索运行的 Provider 结果及最终上下文选择。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return RetrievalRunTrace(
            raw_hit_count=self.raw_hit_count,
            providers=tuple(provider.to_domain() for provider in self.providers),
            selection_status=self.selection_status,
            context_count=self.context_count,
            trace_version=self.trace_version,
        )

    @classmethod
    def from_domain(cls, value: RetrievalRunTrace) -> RetrievalRunTraceSchema:
        """将检索追踪领域对象转换为对应的 schema。

        参数：
        - `value`（RetrievalRunTrace）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - RetrievalRunTraceSchema：记录一次检索运行的 Provider 结果及最终上下文选择。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return cls(
            trace_version=value.trace_version,
            raw_hit_count=value.raw_hit_count,
            providers=tuple(
                RetrievalProviderTraceSchema.from_domain(provider)
                for provider in value.providers
            ),
            selection_status=value.selection_status,
            context_count=value.context_count,
        )


__all__ = [
    "RetrievalCandidateSelectionSchema",
    "RetrievalCandidateTraceSchema",
    "RetrievalChannelTraceSchema",
    "RetrievalHitTraceSchema",
    "RetrievalProviderTraceSchema",
    "RetrievalRunTraceSchema",
]
