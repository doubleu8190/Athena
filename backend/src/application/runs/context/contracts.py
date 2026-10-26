from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain.retrieval.trace_contracts import (
    RetrievalCandidateSelection,
    RetrievalCandidateTrace,
    RetrievalChannelTrace,
)


class MemoryQuery(BaseModel):
    """面向长期记忆的查询。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["memory"]
    query: str = Field(min_length=1, max_length=500)

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("context_query_required")
        return normalized


class EpisodeQuery(BaseModel):
    """面向当前会话任务回顾的查询。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["episode"]
    query: str = Field(min_length=1, max_length=500)
    include_sources: bool = False

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("context_query_required")
        return normalized


class FileQuery(BaseModel):
    """面向当前有效附件的查询。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["file"]
    query: str = Field(min_length=1, max_length=500)
    file_ids: tuple[str, ...] = ()

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("context_query_required")
        return normalized


class KnowledgeQuery(BaseModel):
    """面向当前配置知识库的查询。

    knowledge_base_ids 为 None 时使用全部可用知识库，空元组表示明确的空范围。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["knowledge"]
    query: str = Field(min_length=1, max_length=500)
    knowledge_base_ids: tuple[str, ...] | None = None

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("context_query_required")
        return normalized


class GraphQuery(BaseModel):
    """面向已装配图检索能力的查询。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["graph"]
    query: str = Field(min_length=1, max_length=500)

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("context_query_required")
        return normalized


ContextQuery = Annotated[
    MemoryQuery | EpisodeQuery | FileQuery | KnowledgeQuery | GraphQuery,
    Field(discriminator="provider"),
]

RetrievalStage = Literal["prefetch", "directed"]


class ContextQueryOutcome(BaseModel):
    """供后续 Root 决策参考的一条轻量检索结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    stage: RetrievalStage
    query: ContextQuery
    status: Literal["succeeded", "empty", "failed", "timeout", "skipped"]
    result_count: int = Field(ge=0)
    error_message: str | None = Field(default=None, max_length=500)


@dataclass(frozen=True, slots=True)
class ContextPlan:
    providers: tuple[str, ...] = ("conversation",)
    memory_query: str = ""
    episode_query: str = ""
    episode_include_sources: bool = False
    knowledge_query: str = ""
    graph_query: str = ""
    file_query: str = ""
    file_ids: tuple[str, ...] = ()
    knowledge_base_ids: tuple[str, ...] = ()
    max_files: int = 5
    limit_per_file: int = 5
    max_items: int = 20
    max_tokens: int = 4000
    memory_token_budget: int = 2000
    recall_limit: int = 30
    rerank_limit: int = 10
    provider_timeout_seconds: float | None = None

    def with_updates(self, **changes: Any) -> "ContextPlan":
        """Return a plan with selected provider-specific values replaced."""
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class ContextItem:
    provider: str
    content: str
    source_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider: str
    status: str
    items: tuple[ContextItem, ...] = ()
    error_message: str | None = None
    candidate_count: int = 0
    result_count: int = 0
    duration_ms: float = 0.0
    candidates: tuple[RetrievalCandidateTrace, ...] = ()
    query: str | None = None
    resource_scope: dict[str, Any] = field(default_factory=dict)
    effective_params: dict[str, Any] = field(default_factory=dict)
    channels: tuple[RetrievalChannelTrace, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextBundle:
    items: tuple[ContextItem, ...]
    provider_results: tuple[ProviderResult, ...]
    providers_succeeded: tuple[str, ...] = ()
    providers_failed: tuple[str, ...] = ()
    truncated: bool = False
    retrieval_run_id: str | None = None
    context_selection: tuple[RetrievalCandidateSelection, ...] = ()


__all__ = [
    "ContextBundle",
    "ContextItem",
    "ContextPlan",
    "ContextQuery",
    "ContextQueryOutcome",
    "EpisodeQuery",
    "FileQuery",
    "GraphQuery",
    "KnowledgeQuery",
    "MemoryQuery",
    "ProviderResult",
    "RetrievalStage",
]
