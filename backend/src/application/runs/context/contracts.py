"""定义上下文检索计划、条目、Provider 结果和裁剪后资料包。
"""

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
        """去除查询首尾空白，并将空字符串规范为 None。

        参数：
        - `value`（str）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - str：去除首尾空白且符合查询契约的文本。

        异常：
        - ValueError：校验条件 `not normalized` 成立时（异常说明：context_query_required）。
        - 未列出的依赖异常按当前实现传播。
        """
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
        """去除查询首尾空白，并将空字符串规范为 None。

        参数：
        - `value`（str）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - str：去除首尾空白且符合查询契约的文本。

        异常：
        - ValueError：校验条件 `not normalized` 成立时（异常说明：context_query_required）。
        - 未列出的依赖异常按当前实现传播。
        """
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
        """去除查询首尾空白，并将空字符串规范为 None。

        参数：
        - `value`（str）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - str：去除首尾空白且符合查询契约的文本。

        异常：
        - ValueError：校验条件 `not normalized` 成立时（异常说明：context_query_required）。
        - 未列出的依赖异常按当前实现传播。
        """
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
        """去除查询首尾空白，并将空字符串规范为 None。

        参数：
        - `value`（str）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - str：去除首尾空白且符合查询契约的文本。

        异常：
        - ValueError：校验条件 `not normalized` 成立时（异常说明：context_query_required）。
        - 未列出的依赖异常按当前实现传播。
        """
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
        """去除查询首尾空白，并将空字符串规范为 None。

        参数：
        - `value`（str）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - str：去除首尾空白且符合查询契约的文本。

        异常：
        - ValueError：校验条件 `not normalized` 成立时（异常说明：context_query_required）。
        - 未列出的依赖异常按当前实现传播。
        """
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
    """限定检索 Provider、查询、资源范围和上下文预算的不可变计划。
    """
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
        """返回替换指定 Provider 值后的计划副本。

        参数：
        - `changes`（Any）：用于覆盖 ContextPlan 字段的更新值；约束：无额外运行时限制。

        返回：
        - 'ContextPlan'：将指定 Provider 更新后的不可变 ContextPlan 副本。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class ContextItem:
    """由单个 Provider 返回并带来源元数据的上下文条目。
    """
    provider: str
    content: str
    source_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """记录单个 Provider 的执行状态、候选、错误和追踪信息。
    """
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
    """经过条数和 token 预算裁剪的上下文及选择追踪。
    """
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
