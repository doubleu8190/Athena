"""任务理解阶段产生的规范化规格模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ContextRequirement = Literal["conversation", "memory", "knowledge", "graph", "file"]
TaskMode = Literal["answer", "retrieve", "generate", "act", "plan", "clarify"]


class QueryHints(BaseModel):
    """按检索通道保存查询改写结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    memory: str = ""
    knowledge: str = ""
    graph: str = ""
    file: str = ""

    @field_validator("memory", "knowledge", "graph", "file", mode="before")
    @classmethod
    def normalize_text(cls, value: Any) -> str:
        return str(value or "").strip()[:500]


class OutputSpec(BaseModel):
    """任务结果的内容类型和交付方式。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    content_type: str = "text"
    format: str = "chat"
    target: Literal["inline", "file", "inline_and_file"] = "inline"


class UserTaskSpec(BaseModel):
    """意图识别后的规范化任务描述。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    goal: str = Field(min_length=1, max_length=1000)
    domain: str = "general"
    mode: TaskMode = "answer"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    context_requirements: tuple[ContextRequirement, ...] = ("conversation",)
    slots: dict[str, Any] = Field(default_factory=dict)
    output: OutputSpec = Field(default_factory=OutputSpec)
    constraints: tuple[str, ...] = ()
    requires_clarification: bool = False
    clarification_question: str | None = None
    query_hints: QueryHints = Field(default_factory=QueryHints)
    attachment_ids: tuple[str, ...] = ()
    knowledge_base_ids: tuple[str, ...] = ()

    @field_validator("goal", mode="before")
    @classmethod
    def normalize_goal(cls, value: Any) -> str:
        return str(value or "").strip()[:1000]

    @field_validator(
        "context_requirements", "attachment_ids", "knowledge_base_ids", "constraints", mode="before"
    )
    @classmethod
    def normalize_sequence(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = (value,)
        return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))

    @model_validator(mode="before")
    @classmethod
    def validate_routing(cls, values: Any) -> Any:
        payload = dict(values or {})
        requirements = list(payload.get("context_requirements") or ())
        attachments = payload.get("attachment_ids") or ()
        if attachments and "file" not in requirements:
            requirements.append("file")
        if not requirements:
            requirements.append("conversation")
        if payload.get("mode") == "retrieve" and not any(
            item in requirements for item in ("memory", "knowledge", "graph", "file")
        ):
            requirements.append("knowledge")
        requires_clarification = bool(payload.get("requires_clarification")) or payload.get("mode") == "clarify"
        question = str(payload.get("clarification_question") or "").strip() or None
        if requires_clarification and not question:
            question = "请补充完成该任务所需的关键信息。"
        payload.update(
            {
                "context_requirements": tuple(dict.fromkeys(requirements)),
                "mode": "clarify" if requires_clarification else payload.get("mode", "answer"),
                "requires_clarification": requires_clarification,
                "clarification_question": question if requires_clarification else None,
            }
        )
        return payload


__all__ = ["ContextRequirement", "OutputSpec", "QueryHints", "TaskMode", "UserTaskSpec"]
