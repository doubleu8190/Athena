"""记忆 JSON 边界 schema 及领域对象转换。"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Annotated, Literal, overload

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain.memory.contracts import (
    MemoryCandidate as DomainMemoryCandidate,
    MemoryEpisode as DomainMemoryEpisode,
    MemoryEpisodeProposal as DomainMemoryEpisodeProposal,
    MemoryEpisodeSummary as DomainMemoryEpisodeSummary,
    MemoryExecutionEvidence as DomainMemoryExecutionEvidence,
    MemoryFact as DomainMemoryFact,
    MemoryFactIdentity as DomainMemoryFactIdentity,
    MemoryFactExtractionSnapshot as DomainMemoryFactExtractionSnapshot,
    MemoryRecordMetadata as DomainMemoryRecordMetadata,
    MemorySourceReference as DomainMemorySourceReference,
    MemoryTaskOutcomeEvidence as DomainMemoryTaskOutcomeEvidence,
    MemoryToolOutcomeEvidence as DomainMemoryToolOutcomeEvidence,
)


class MemorySourceReferenceSchema(BaseModel):
    """指向原始对话或执行证据的引用。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_type: Literal["turn", "run", "worker_message", "artifact"]
    source_id: str = Field(min_length=1)
    excerpt: str = Field(min_length=1, max_length=2000)
    observed_at: str


class MemoryFactSchema(BaseModel):
    """已提取事实的主体、适用范围、值与来源。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    scope_kind: Literal["global", "project", "session"]
    scope_id: str = Field(min_length=1)
    subject_key: str = Field(min_length=1, max_length=100)
    fact_key: str = Field(min_length=1, max_length=30, pattern=r"^[a-z][a-z0-9_]*$")
    fact_value: str | None = Field(min_length=1, max_length=500)
    retracts_value: str | None = Field(default=None, min_length=1, max_length=500)
    polarity: Literal["affirmed", "negated"] = "affirmed"
    claim_intent: Literal["assert", "correct", "retract"] = "assert"
    confidence: float = Field(ge=0.6, le=1.0)
    asserted_at: str
    valid_from: str | None = None
    valid_until: str | None = None
    source_refs: tuple[MemorySourceReferenceSchema, ...] = Field(min_length=1)

    @field_validator("source_refs", mode="before")
    @classmethod
    def _freeze_source_refs(cls, value: object) -> object:
        """Normalize JSON-decoded arrays at the metadata persistence boundary."""
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def validate_retraction_value(self) -> "MemoryFactSchema":
        """撤回记录不携带新值，其他事实必须有明确值。"""
        valid_retraction = (
            self.claim_intent == "retract"
            and self.fact_value is None
            and self.retracts_value is not None
        )
        valid_assertion = (
            self.claim_intent != "retract"
            and self.fact_value is not None
            and self.retracts_value is None
        )
        if not (valid_retraction or valid_assertion):
            raise ValueError("memory_fact_retraction_value_invalid")
        return self


class MemoryFactIdentitySchema(BaseModel):
    """用于精确复核事实组的稳定身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    scope_kind: Literal["global", "project", "session"]
    scope_id: str = Field(min_length=1)
    subject_key: str = Field(min_length=1, max_length=100)
    fact_key: str = Field(min_length=1, max_length=30, pattern=r"^[a-z][a-z0-9_]*$")


class MemoryToolOutcomeEvidenceSchema(BaseModel):
    """Worker 工具回执的最小、可追溯结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    message_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1, max_length=120)
    status: str = Field(min_length=1, max_length=40)
    result_ref: str | None = None
    observed_at: str


class MemoryTaskOutcomeEvidenceSchema(BaseModel):
    """一次任务最终状态及其有限的工具证据。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=300)
    status: str = Field(min_length=1, max_length=40)
    execution_generation: int = Field(ge=0)
    error_code: str | None = Field(default=None, max_length=100)
    result_ref: str | None = None
    tool_outcomes: tuple[MemoryToolOutcomeEvidenceSchema, ...] = Field(max_length=4)


class MemoryExecutionEvidenceSchema(BaseModel):
    """Run 的冻结执行摘要，只存状态和来源引用，不复制完整工具输出。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str = Field(min_length=1)
    source_fingerprint: str = Field(min_length=64, max_length=64)
    total_task_count: int = Field(ge=0)
    truncated: bool
    tasks: tuple[MemoryTaskOutcomeEvidenceSchema, ...] = Field(max_length=12)
    source_refs: tuple[MemorySourceReferenceSchema, ...] = Field(max_length=80)


class MemoryEpisodeSummarySchema(BaseModel):
    """任务回顾的可检索摘要与验证边界。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    title: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=800)
    outcome: Literal["completed", "partial", "failed", "cancelled", "unknown"]
    result: str = Field(min_length=1, max_length=1500)
    attempts: str = Field(default="", max_length=1000)
    reusable_learnings: tuple[Annotated[str, Field(max_length=200)], ...] = Field(
        default=(), max_length=8
    )
    unresolved: tuple[Annotated[str, Field(max_length=200)], ...] = Field(
        default=(), max_length=8
    )


class MemoryEpisodeProposalSchema(BaseModel):
    """模型提交的任务回顾及其允许引用的真实来源 ID。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    retain: bool
    reason_code: str | None = Field(default=None, max_length=100)
    summary: MemoryEpisodeSummarySchema | None = None
    source_ref_ids: tuple[str, ...] = Field(max_length=80)

    @model_validator(mode="after")
    def validate_retained_episode(self) -> "MemoryEpisodeProposalSchema":
        """只保留同时具有摘要和来源的回顾。"""
        if self.retain and (self.summary is None or not self.source_ref_ids):
            raise ValueError("memory_episode_sources_required")
        if not self.retain and (self.summary is not None or self.source_ref_ids):
            raise ValueError("memory_episode_discard_payload_invalid")
        if len(set(self.source_ref_ids)) != len(self.source_ref_ids):
            raise ValueError("memory_episode_duplicate_source_ref")
        return self


class MemoryEpisodeSchema(BaseModel):
    """持久化任务回顾，来源只能由程序从冻结证据中解析。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    episode_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    project_id: str | None = Field(default=None, min_length=1)
    content: str = Field(min_length=1, max_length=8000)
    summary: MemoryEpisodeSummarySchema
    source_refs: tuple[MemorySourceReferenceSchema, ...] = Field(
        min_length=1, max_length=80
    )
    source_fingerprint: str = Field(min_length=64, max_length=64)
    created_at: str


class MemoryFactProposalSchema(BaseModel):
    """模型从一轮对话中提出的事实字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: str = Field(min_length=1, max_length=30, pattern=r"^[a-z][a-z0-9_]*$")
    value: str | None = Field(min_length=1, max_length=500)
    retracts_value: str | None = Field(default=None, min_length=1, max_length=500)
    category: Literal[
        "preference",
        "profile",
        "project",
        "technical_decision",
        "fact",
        "solution",
        "unresolved",
        "other",
    ]
    scope_kind: Literal["global", "project"]
    confidence: float = Field(ge=0.0, le=1.0)
    polarity: Literal["affirmed", "negated"] = "affirmed"
    claim_intent: Literal["assert", "correct", "retract"] = "assert"

    @model_validator(mode="after")
    def validate_retraction_value(self) -> "MemoryFactProposalSchema":
        """只允许撤回候选省略新值。"""
        valid_retraction = (
            self.claim_intent == "retract"
            and self.value is None
            and self.retracts_value is not None
        )
        valid_assertion = (
            self.claim_intent != "retract"
            and self.value is not None
            and self.retracts_value is None
        )
        if not (valid_retraction or valid_assertion):
            raise ValueError("memory_proposal_retraction_value_invalid")
        return self


class MemoryFactExtractionOutputSchema(BaseModel):
    """严格校验的大模型事实提取结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    atomic_facts: tuple[MemoryFactProposalSchema, ...] = Field(max_length=20)


class MemoryFactExtractionSnapshotSchema(BaseModel):
    """一次轮次的冻结提取清单；空列表也表示已成功提取。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    turn_id: str = Field(min_length=1)
    candidates: tuple["MemoryCandidateSchema", ...]


class MemoryCandidateSchema(BaseModel):
    """可稳定写入作业快照的单条候选。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    content: str = Field(min_length=1, max_length=600)
    source_turn_id: str = Field(min_length=1)
    memory_type: str = "fact"
    category: str = "fact"
    confidence: float = Field(ge=0.6, le=1.0)
    session_id: str = ""
    fact: MemoryFactSchema

MemoryFactExtractionSnapshotSchema.model_rebuild()


class MemoryRecordMetadataSchema(BaseModel):
    """新记忆记录的稳定字段与开放扩展字段。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    fact: MemoryFactSchema | None = None


_SCHEMA_TO_DOMAIN = {
    MemorySourceReferenceSchema: DomainMemorySourceReference,
    MemoryFactSchema: DomainMemoryFact,
    MemoryFactIdentitySchema: DomainMemoryFactIdentity,
    MemoryToolOutcomeEvidenceSchema: DomainMemoryToolOutcomeEvidence,
    MemoryTaskOutcomeEvidenceSchema: DomainMemoryTaskOutcomeEvidence,
    MemoryExecutionEvidenceSchema: DomainMemoryExecutionEvidence,
    MemoryEpisodeSummarySchema: DomainMemoryEpisodeSummary,
    MemoryEpisodeProposalSchema: DomainMemoryEpisodeProposal,
    MemoryEpisodeSchema: DomainMemoryEpisode,
    MemoryFactExtractionSnapshotSchema: DomainMemoryFactExtractionSnapshot,
    MemoryCandidateSchema: DomainMemoryCandidate,
    MemoryRecordMetadataSchema: DomainMemoryRecordMetadata,
}
_DOMAIN_TO_SCHEMA = {
    domain_type: schema_type for schema_type, domain_type in _SCHEMA_TO_DOMAIN.items()
}


@overload
def memory_schema_to_domain(
    value: MemorySourceReferenceSchema,
) -> DomainMemorySourceReference: ...


@overload
def memory_schema_to_domain(value: MemoryFactSchema) -> DomainMemoryFact: ...


@overload
def memory_schema_to_domain(
    value: MemoryFactIdentitySchema,
) -> DomainMemoryFactIdentity: ...


@overload
def memory_schema_to_domain(
    value: MemoryToolOutcomeEvidenceSchema,
) -> DomainMemoryToolOutcomeEvidence: ...


@overload
def memory_schema_to_domain(
    value: MemoryTaskOutcomeEvidenceSchema,
) -> DomainMemoryTaskOutcomeEvidence: ...


@overload
def memory_schema_to_domain(
    value: MemoryExecutionEvidenceSchema,
) -> DomainMemoryExecutionEvidence: ...


@overload
def memory_schema_to_domain(
    value: MemoryEpisodeSummarySchema,
) -> DomainMemoryEpisodeSummary: ...


@overload
def memory_schema_to_domain(
    value: MemoryEpisodeProposalSchema,
) -> DomainMemoryEpisodeProposal: ...


@overload
def memory_schema_to_domain(value: MemoryEpisodeSchema) -> DomainMemoryEpisode: ...


@overload
def memory_schema_to_domain(
    value: MemoryFactExtractionSnapshotSchema,
) -> DomainMemoryFactExtractionSnapshot: ...


@overload
def memory_schema_to_domain(
    value: MemoryCandidateSchema,
) -> DomainMemoryCandidate: ...


@overload
def memory_schema_to_domain(
    value: MemoryRecordMetadataSchema,
) -> DomainMemoryRecordMetadata: ...


def memory_schema_to_domain(value: object) -> object:
    """把已校验的记忆边界 schema 递归转换为领域 dataclass。"""
    if isinstance(value, BaseModel):
        domain_type = _SCHEMA_TO_DOMAIN.get(type(value))
        if domain_type is None:
            raise TypeError(f"memory_schema_has_no_domain_type:{type(value).__name__}")
        values = {}
        for item in fields(domain_type):
            if item.name == "extra_fields":
                values[item.name] = dict(value.model_extra or {})
            else:
                values[item.name] = memory_schema_to_domain(getattr(value, item.name))
        return domain_type(**values)
    if isinstance(value, tuple):
        return tuple(memory_schema_to_domain(item) for item in value)
    if isinstance(value, list):
        return [memory_schema_to_domain(item) for item in value]
    if isinstance(value, dict):
        return {key: memory_schema_to_domain(item) for key, item in value.items()}
    return value


def memory_domain_to_schema(value: object) -> BaseModel:
    """把领域记忆对象递归转换为待持久化或外发的 Pydantic schema。"""
    if not is_dataclass(value):
        raise TypeError(f"memory_domain_type_not_supported:{type(value).__name__}")
    schema_type = _DOMAIN_TO_SCHEMA.get(type(value))
    if schema_type is None:
        raise TypeError(f"memory_domain_type_has_no_schema:{type(value).__name__}")
    values = {}
    for item in fields(value):
        if item.name == "extra_fields":
            continue
        values[item.name] = _domain_value_to_schema_value(getattr(value, item.name))
    if isinstance(value, DomainMemoryRecordMetadata):
        values.update(
            {
                key: _domain_value_to_schema_value(item)
                for key, item in value.extra_fields.items()
            }
        )
    return schema_type.model_validate(values)


def _domain_value_to_schema_value(value: object) -> object:
    """递归转换嵌套领域对象和不可变集合。"""
    if is_dataclass(value):
        return memory_domain_to_schema(value)
    if isinstance(value, tuple):
        return tuple(_domain_value_to_schema_value(item) for item in value)
    if isinstance(value, list):
        return [_domain_value_to_schema_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _domain_value_to_schema_value(item) for key, item in value.items()}
    return value


__all__ = [
    "MemoryCandidateSchema",
    "MemoryEpisodeSchema",
    "MemoryEpisodeProposalSchema",
    "MemoryEpisodeSummarySchema",
    "MemoryExecutionEvidenceSchema",
    "MemoryFactSchema",
    "MemoryFactIdentitySchema",
    "MemoryFactExtractionOutputSchema",
    "MemoryFactExtractionSnapshotSchema",
    "MemoryFactProposalSchema",
    "MemoryRecordMetadataSchema",
    "MemorySourceReferenceSchema",
    "MemoryTaskOutcomeEvidenceSchema",
    "MemoryToolOutcomeEvidenceSchema",
    "memory_domain_to_schema",
    "memory_schema_to_domain",
]
