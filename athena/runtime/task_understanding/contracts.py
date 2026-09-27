"""通用任务理解与上下文获取契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TaskDomain = Literal[
    "writing", "analysis", "research", "planning", "coding",
    "data_processing", "document_editing", "resource_retrieval", "general",
]
TaskMode = Literal["answer", "retrieve", "generate", "act", "plan", "clarify"]
ContextRequirement = Literal["conversation", "memory", "knowledge", "file"]
ContentType = Literal[
    "document", "presentation", "spreadsheet", "report", "summary",
    "code", "data", "general_text",
]
OutputFormat = Literal["chat", "markdown", "docx", "pptx", "xlsx", "pdf", "txt"]
OutputTarget = Literal["inline", "file", "inline_and_file"]
ProviderStatus = Literal["succeeded", "failed", "timeout", "skipped"]


class TaskSlots(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=480)
    difficulty: str | None = None
    item_count: int | None = Field(default=None, ge=1, le=200)
    audience: str | None = None
    target_file_ids: list[str] = Field(default_factory=list)
    knowledge_base_ids: list[str] = Field(default_factory=list)


class OutputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_type: ContentType | None = None
    format: OutputFormat = "chat"
    target: OutputTarget = "inline"


class TaskQueryHints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory: str | None = Field(default=None, max_length=500)
    knowledge: str | None = Field(default=None, max_length=500)
    file: str | None = Field(default=None, max_length=500)


class UserTaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=1000)
    domain: TaskDomain
    mode: TaskMode
    confidence: float = Field(ge=0, le=1)
    context_requirements: list[ContextRequirement] = Field(default_factory=list)
    slots: TaskSlots = Field(default_factory=TaskSlots)
    output: OutputSpec = Field(default_factory=OutputSpec)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    query_hints: TaskQueryHints = Field(default_factory=TaskQueryHints)
    requires_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_spec(self) -> "UserTaskSpec":
        if self.requires_clarification != (self.mode == "clarify"):
            raise ValueError("clarify mode and requires_clarification must agree")
        if self.requires_clarification and not self.clarification_question:
            raise ValueError("clarification_question is required")
        if not self.requires_clarification and self.clarification_question:
            raise ValueError("clarification_question must be empty")
        if self.mode == "retrieve" and not any(
            item in self.context_requirements for item in ("memory", "knowledge", "file")
        ):
            raise ValueError("retrieve mode requires a retrieval context")
        return self


class ContextPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: list[ContextRequirement] = Field(default_factory=list)
    memory_query: str | None = None
    knowledge_query: str | None = None
    file_query: str | None = None
    file_ids: list[str] = Field(default_factory=list)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    max_files: int = Field(default=5, ge=1, le=20)
    limit_per_file: int = Field(default=5, ge=1, le=20)
    max_items: int = Field(default=12, ge=1, le=50)
    max_tokens: int = Field(default=4000, ge=100, le=20000)


class ContextItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ContextRequirement
    content: str
    source_id: str | None = None
    retrieval_run_id: str | None = None
    title: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ContextRequirement
    status: ProviderStatus
    items: list[ContextItem] = Field(default_factory=list)
    candidate_count: int = 0
    result_count: int = 0
    duration_ms: int = 0
    error_message: str | None = None


class ContextBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ContextItem] = Field(default_factory=list)
    provider_results: list[ProviderResult] = Field(default_factory=list)
    providers_succeeded: list[str] = Field(default_factory=list)
    providers_failed: list[str] = Field(default_factory=list)
    truncated: bool = False
