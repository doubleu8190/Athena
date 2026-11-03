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
    ContextQueryOutcome,
)
from application.runs.request_contracts import RootAttachment, RootKnowledgeBase
from .root_completed_plan import RootCompletedPlanModel


class RootMessage(BaseModel):
    """供 Root 阅读的一条历史或当前对话消息。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str


class RootWorkerTool(BaseModel):
    """由系统注册并启用的 Worker 工具说明。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    description: str
    parameters: dict[str, JsonValue]
    risk_level: Literal["low", "medium", "high"]
    requires_approval: bool


class RootWorkerCapability(BaseModel):
    """提供给 Root 的已授权能力契约，不包含实例路由或运行参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: str = Field(min_length=1)
    contract_revision: int = Field(strict=True, ge=1)
    can_do: tuple[str, ...] = Field(min_length=1)
    cannot_do: tuple[str, ...] = Field(min_length=1)
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]


class PlanningFeedback(BaseModel):
    """计划生成发现的必要信息缺口。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["needs_information"]
    missing_information: tuple[str, ...] = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("missing_information")
    @classmethod
    def non_empty_missing_information(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        """去除空白项并规范化待补充信息列表。

        参数：
        - `values`（tuple[str, ...]）：待转换或校验的值集合；约束：无额外运行时限制。

        返回：
        - tuple[str, ...]：去除空项并保留原顺序的待补充信息列表。

        异常：
        - ValueError：校验条件 `any((not value for value in normalized))` 成立时（异常说明：root_planning_feedback_item_required）。
        - 未列出的依赖异常按当前实现传播。
        """
        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("root_planning_feedback_item_required")
        return normalized

    @field_validator("reason")
    @classmethod
    def non_empty_reason(cls, value: str) -> str:
        """去除首尾空白并拒绝空的阻塞原因。

        参数：
        - `value`（str）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - str：去除首尾空白且不能为空的决策原因。

        异常：
        - ValueError：校验条件 `not normalized` 成立时（异常说明：root_planning_feedback_reason_required）。
        - 未列出的依赖异常按当前实现传播。
        """
        normalized = value.strip()
        if not normalized:
            raise ValueError("root_planning_feedback_reason_required")
        return normalized


class RootAgentInput(BaseModel):
    """供一次 Root 决策使用的消息、资源和当前循环状态。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    messages: tuple[RootMessage, ...] = Field(min_length=1)
    attachments: list[RootAttachment] = Field(default_factory=list)
    knowledge_bases: list[RootKnowledgeBase] = Field(default_factory=list)
    worker_capabilities: tuple[RootWorkerCapability, ...] = ()
    completed_plans: tuple[RootCompletedPlanModel, ...] = ()
    available_context_providers: tuple[
        Literal["memory", "episode", "file", "knowledge", "graph"], ...
    ] = ()
    context_items: tuple[ContextItem, ...] = ()
    context_queries: tuple[ContextQueryOutcome, ...] = ()
    remaining_turns: int = Field(ge=1)
    planning_feedback: PlanningFeedback | None = None
    planning_context_updated: bool = False


class AnswerIntent(BaseModel):
    """Root 选择的回答类型及正文生成要求。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["answer"]
    kind: Literal["final", "clarification"]
    instructions: str = Field(min_length=1, max_length=2000)

    @field_validator("instructions")
    @classmethod
    def non_empty_instructions(cls, value: str) -> str:
        """去掉生成要求的首尾空白并拒绝空内容。

        参数：
        - `value`（str）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - str：去除空项并保留原顺序的执行指令列表。

        异常：
        - ValueError：校验条件 `not normalized` 成立时（异常说明：root_answer_instructions_required）。
        - 未列出的依赖异常按当前实现传播。
        """
        normalized = value.strip()
        if not normalized:
            raise ValueError("root_answer_instructions_required")
        return normalized


class PlanIntent(BaseModel):
    """Root 选择计划生成并提供目标与简短约束。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["plan"]
    goal: str = Field(min_length=1)
    instructions: str = Field(min_length=1, max_length=2000)

    @field_validator("goal", "instructions")
    @classmethod
    def non_empty_plan_text(cls, value: str) -> str:
        """去掉计划文字首尾空白并拒绝空值。

        参数：
        - `value`（str）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - str：去除首尾空白且不能为空的计划说明文本。

        异常：
        - ValueError：校验条件 `not normalized` 成立时（异常说明：root_plan_intent_text_required）。
        - 未列出的依赖异常按当前实现传播。
        """
        normalized = value.strip()
        if not normalized:
            raise ValueError("root_plan_intent_text_required")
        return normalized


class RootPlanInput(BaseModel):
    """独立计划生成所需的请求、资料和执行能力。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    messages: tuple[RootMessage, ...] = Field(min_length=1)
    attachments: list[RootAttachment] = Field(default_factory=list)
    knowledge_bases: list[RootKnowledgeBase] = Field(default_factory=list)
    worker_capabilities: tuple[RootWorkerCapability, ...] = ()
    context_items: tuple[ContextItem, ...] = ()
    context_queries: tuple[ContextQueryOutcome, ...] = ()
    goal: str = Field(min_length=1)
    instructions: str = Field(min_length=1, max_length=2000)
    completed_plans: tuple[RootCompletedPlanModel, ...] = ()


class PlanTaskDraft(BaseModel):
    """一个交给 Worker 执行的任务草稿。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_ref: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    contract_revision: int = Field(strict=True, ge=1)
    objective: str = Field(min_length=1)
    input: dict[str, JsonValue]
    acceptance_criteria: str = Field(min_length=1)

    @field_validator("task_ref", "contract_id", "objective", "acceptance_criteria")
    @classmethod
    def non_empty_task_text(cls, value: str) -> str:
        """去掉必填任务文本的首尾空白。

        参数：
        - `value`（str）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - str：去除首尾空白且不能为空的 Task 描述文本。

        异常：
        - ValueError：校验条件 `not normalized` 成立时（异常说明：root_plan_task_text_required）。
        - 未列出的依赖异常按当前实现传播。
        """
        normalized = value.strip()
        if not normalized:
            raise ValueError("root_plan_task_text_required")
        return normalized



class PlanDraft(BaseModel):
    """经过结构校验、尚未提交的执行计划。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["plan"]
    goal: str = Field(min_length=1)
    tasks: tuple[PlanTaskDraft, ...] = Field(min_length=1)

    @field_validator("tasks")
    @classmethod
    def task_refs_are_unique(cls, tasks: tuple[PlanTaskDraft, ...]) -> tuple[PlanTaskDraft, ...]:
        """拒绝同一 Plan 中重复的任务引用。"""
        refs = tuple(task.task_ref for task in tasks)
        if len(refs) != len(set(refs)):
            raise ValueError("root_plan_task_ref_duplicate")
        return tasks

    @field_validator("goal")
    @classmethod
    def non_empty_goal(cls, value: str) -> str:
        """去掉计划目标的首尾空白并拒绝空目标。

        参数：
        - `value`（str）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - str：去除首尾空白且不能为空的计划目标文本。

        异常：
        - ValueError：校验条件 `not normalized` 成立时（异常说明：root_plan_goal_required）。
        - 未列出的依赖异常按当前实现传播。
        """
        normalized = value.strip()
        if not normalized:
            raise ValueError("root_plan_goal_required")
        return normalized


class RootDecisionStructuredOutput(BaseModel):
    """保留扁平 action 契约的 LangChain 结构化输出 schema。

    各 action 的必填字段由 ``ROOT_DECISION_ADAPTER`` 再按判别联合严格校验。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["answer", "plan"]
    kind: Literal["final", "clarification"] | None = None
    instructions: str | None = None
    goal: str | None = None

class RootPlanStructuredOutput(BaseModel):
    """完整计划或规划信息缺口的扁平结构化 schema。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["plan", "needs_information"]
    goal: str | None = None
    tasks: tuple[PlanTaskDraft, ...] | None = None
    missing_information: tuple[str, ...] | None = None
    reason: str | None = None


RootDecision = Annotated[
    AnswerIntent | PlanIntent, Field(discriminator="action")
]
ROOT_DECISION_ADAPTER = TypeAdapter(RootDecision)
RootPlanResult = Annotated[PlanDraft | PlanningFeedback, Field(discriminator="action")]
ROOT_PLAN_ADAPTER = TypeAdapter(RootPlanResult)


__all__ = [
    "AnswerIntent",
    "PlanDraft",
    "PlanIntent",
    "PlanTaskDraft",
    "PlanningFeedback",
    "RootDecisionStructuredOutput",
    "RootPlanStructuredOutput",
    "ROOT_DECISION_ADAPTER",
    "ROOT_PLAN_ADAPTER",
    "RootAgentInput",
    "RootAttachment",
    "RootDecision",
    "RootPlanInput",
    "RootPlanResult",
    "RootKnowledgeBase",
    "RootMessage",
    "RootWorkerTool",
    "RootWorkerCapability",
]
