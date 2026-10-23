"""Root 初始决策使用的具体输入、输出契约。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
)

from application.runs.context.contracts import (
    ContextItem,
    ContextQuery,
    ContextQueryOutcome,
)


class RootMessage(BaseModel):
    """供 Root 阅读的一条历史或当前对话消息。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str


class RootAttachment(BaseModel):
    """当前请求中已验证的附件元数据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    filename: str
    mime_type: str
    size_bytes: int = Field(ge=0)
    status: Literal["ready"]


class RootKnowledgeBase(BaseModel):
    """Root 可选择的知识库描述和可用文档数量。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    name: str
    description: str = ""
    document_count: int = Field(ge=0)
    ready_document_count: int = Field(ge=0)


class RootWorkerTool(BaseModel):
    """由系统注册并启用的 Worker 工具说明。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    description: str
    parameters: dict[str, JsonValue]
    risk_level: Literal["low", "medium", "high"]
    requires_approval: bool


class RootAgentInput(BaseModel):
    """供一次 Root 决策使用的消息、资源和当前循环状态。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    messages: tuple[RootMessage, ...] = Field(min_length=1)
    attachments: tuple[RootAttachment, ...] = ()
    knowledge_bases: tuple[RootKnowledgeBase, ...] = ()
    worker_tools: tuple[RootWorkerTool, ...] = ()
    available_context_providers: tuple[
        Literal["memory", "file", "knowledge", "graph"], ...
    ] = ()
    context_items: tuple[ContextItem, ...] = ()
    context_queries: tuple[ContextQueryOutcome, ...] = ()
    remaining_turns: int = Field(ge=1)


class Answer(BaseModel):
    """Root 对用户的最终答复或澄清问题。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["answer"]
    kind: Literal["final", "clarification"]
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def non_empty_content(cls, value: str) -> str:
        """去掉首尾空白并拒绝空答复。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("root_answer_content_required")
        return normalized


class ContextRequest(BaseModel):
    """Root 针对已知资源提出的一批检索查询。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["context"]
    queries: tuple[ContextQuery, ...] = Field(min_length=1, max_length=4)

    @field_validator("queries")
    @classmethod
    def unique_providers(
        cls, queries: tuple[ContextQuery, ...]
    ) -> tuple[ContextQuery, ...]:
        providers = [query.provider for query in queries]
        if len(providers) != len(set(providers)):
            raise ValueError("one_query_per_context_provider")
        return queries


class PlanEdgeDraft(BaseModel):
    """计划草稿中的前置任务关系。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_task_id: str = Field(min_length=1)
    to_task_id: str = Field(min_length=1)

    @field_validator("from_task_id", "to_task_id")
    @classmethod
    def non_empty_task_ids(cls, value: str) -> str:
        """规范化计划边两端的任务 ID。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("root_plan_edge_task_id_required")
        return normalized


class PlanTaskDraft(BaseModel):
    """一个交给 Worker 执行的任务草稿。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    allowed_tools: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    input: dict[str, JsonValue]
    expected_output: dict[str, JsonValue]
    plan_version: int = Field(default=1, strict=True, ge=1)

    @field_validator("task_id", "title", "objective")
    @classmethod
    def non_empty_task_text(cls, value: str) -> str:
        """去掉必填任务文本的首尾空白。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("root_plan_task_text_required")
        return normalized

    @field_validator("allowed_tools")
    @classmethod
    def unique_tool_names(cls, tools: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(tool.strip() for tool in tools)
        if any(not tool for tool in normalized) or len(normalized) != len(
            set(normalized)
        ):
            raise ValueError("plan_task_tools_must_be_unique_non_empty_names")
        return normalized

    @field_validator("depends_on")
    @classmethod
    def unique_dependencies(cls, dependencies: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dependency.strip() for dependency in dependencies)
        if any(not dependency for dependency in normalized) or len(normalized) != len(
            set(normalized)
        ):
            raise ValueError("plan_task_dependencies_must_be_unique_non_empty_ids")
        return normalized


class PlanDraft(BaseModel):
    """经过结构校验、尚未提交的执行计划。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["plan"]
    goal: str = Field(min_length=1)
    tasks: tuple[PlanTaskDraft, ...] = Field(min_length=1)
    edges: tuple[PlanEdgeDraft, ...] = ()
    max_parallelism: int = Field(default=1, strict=True, ge=1)

    @field_validator("goal")
    @classmethod
    def non_empty_goal(cls, value: str) -> str:
        """去掉计划目标的首尾空白并拒绝空目标。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("root_plan_goal_required")
        return normalized


class RootDecisionStructuredOutput(BaseModel):
    """保留扁平 action 契约的 LangChain 结构化输出 schema。

    各 action 的必填字段由 ``ROOT_DECISION_ADAPTER`` 再按判别联合严格校验。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["answer", "context", "plan"]
    kind: Literal["final", "clarification"] | None = None
    content: str | None = None
    queries: tuple[ContextQuery, ...] | None = None
    goal: str | None = None
    tasks: tuple[PlanTaskDraft, ...] | None = None
    edges: tuple[PlanEdgeDraft, ...] | None = None
    max_parallelism: int | None = None


RootDecision = Annotated[Answer | ContextRequest | PlanDraft, Field(discriminator="action")]
ROOT_DECISION_ADAPTER = TypeAdapter(RootDecision)


__all__ = [
    "Answer",
    "ContextRequest",
    "PlanDraft",
    "PlanEdgeDraft",
    "PlanTaskDraft",
    "RootDecisionStructuredOutput",
    "ROOT_DECISION_ADAPTER",
    "RootAgentInput",
    "RootAttachment",
    "RootDecision",
    "RootKnowledgeBase",
    "RootMessage",
    "RootWorkerTool",
]
