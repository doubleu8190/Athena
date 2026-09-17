"""Task Understanding 与 Context Provider 共享的运行时契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ContextRequirement = Literal[
    "conversation",
    "memory",
    "knowledge",
    "file",
    "external",
]

TaskType = Literal[
    "answer",
    "retrieve",
    "act",
    "delegate",
]

ProviderStatus = Literal[
    "succeeded",
    "failed",
    "timeout",
    "skipped",
]


class TaskQueryHints(BaseModel):
    """任务理解产出的检索查询建议。"""

    model_config = ConfigDict(extra="forbid")

    memory: str | None = Field(default=None, max_length=500)
    knowledge: str | None = Field(default=None, max_length=500)


class UserTaskSpec(BaseModel):
    """用户单次请求的任务理解结果。"""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=1000)
    task_type: TaskType
    context_requirements: list[ContextRequirement] = Field(default_factory=list)
    query_hints: TaskQueryHints = Field(default_factory=TaskQueryHints)
    requires_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_spec(self) -> "UserTaskSpec":
        """保证澄清标志与澄清问题成对出现。

        返回值：
            UserTaskSpec: 校验通过的当前对象。

        异常：
            ValueError: 澄清问题与澄清标志不一致。
        """
        if self.requires_clarification and not self.clarification_question:
            raise ValueError("clarification_question is required")
        if not self.requires_clarification and self.clarification_question:
            raise ValueError("clarification_question must be empty")
        return self


class ContextPlan(BaseModel):
    """由 Runtime 推导出的上下文获取计划。"""

    model_config = ConfigDict(extra="forbid")

    providers: list[ContextRequirement] = Field(default_factory=list)
    memory_query: str | None = None
    knowledge_query: str | None = None
    file_ids: list[str] = Field(default_factory=list)
    max_files: int = Field(default=5, ge=1, le=20)
    limit_per_file: int = Field(default=5, ge=1, le=20)
    max_items: int = Field(default=12, ge=1, le=50)
    max_tokens: int = Field(default=4000, ge=100, le=20000)


class ContextItem(BaseModel):
    """注入 Agent 系统提示的一条检索上下文。"""

    model_config = ConfigDict(extra="forbid")

    provider: ContextRequirement
    content: str
    source_id: str | None = None
    title: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderResult(BaseModel):
    """单个 Provider 的执行结果和观测信息。"""

    model_config = ConfigDict(extra="forbid")

    provider: ContextRequirement
    status: ProviderStatus
    items: list[ContextItem] = Field(default_factory=list)
    candidate_count: int = 0
    result_count: int = 0
    duration_ms: int = 0
    error_message: str | None = None


class ContextBundle(BaseModel):
    """所有 Provider 结果合并后的上下文包。"""

    model_config = ConfigDict(extra="forbid")

    items: list[ContextItem] = Field(default_factory=list)
    provider_results: list[ProviderResult] = Field(default_factory=list)
    providers_succeeded: list[str] = Field(default_factory=list)
    providers_failed: list[str] = Field(default_factory=list)
    truncated: bool = False
