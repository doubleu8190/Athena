"""记忆事实、候选快照与解析结果的 JSON 契约。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from .entities import MemoryCandidate


class MemorySourceReference(BaseModel):
    """指向原始对话或执行证据的引用。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_type: Literal["turn", "run", "worker_message", "artifact"]
    source_id: str = Field(min_length=1)
    excerpt: str = Field(min_length=1, max_length=2000)
    observed_at: str


class MemoryFact(BaseModel):
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
    source_refs: tuple[MemorySourceReference, ...] = Field(min_length=1)

    @field_validator("source_refs", mode="before")
    @classmethod
    def _freeze_source_refs(cls, value: object) -> object:
        """Normalize JSON-decoded arrays at the metadata persistence boundary."""
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def validate_retraction_value(self) -> "MemoryFact":
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


class MemoryFactIdentity(BaseModel):
    """用于精确复核事实组的稳定身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    scope_kind: Literal["global", "project", "session"]
    scope_id: str = Field(min_length=1)
    subject_key: str = Field(min_length=1, max_length=100)
    fact_key: str = Field(min_length=1, max_length=30, pattern=r"^[a-z][a-z0-9_]*$")


class MemoryToolOutcomeEvidence(BaseModel):
    """Worker 工具回执的最小、可追溯结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    message_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1, max_length=120)
    status: str = Field(min_length=1, max_length=40)
    result_ref: str | None = None
    observed_at: str


class MemoryTaskOutcomeEvidence(BaseModel):
    """一次任务最终状态及其有限的工具证据。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=300)
    status: str = Field(min_length=1, max_length=40)
    execution_generation: int = Field(ge=0)
    error_code: str | None = Field(default=None, max_length=100)
    result_ref: str | None = None
    tool_outcomes: tuple[MemoryToolOutcomeEvidence, ...] = Field(max_length=4)


class MemoryExecutionEvidence(BaseModel):
    """Run 的冻结执行摘要，只存状态和来源引用，不复制完整工具输出。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str = Field(min_length=1)
    source_fingerprint: str = Field(min_length=64, max_length=64)
    total_task_count: int = Field(ge=0)
    truncated: bool
    tasks: tuple[MemoryTaskOutcomeEvidence, ...] = Field(max_length=12)
    source_refs: tuple[MemorySourceReference, ...] = Field(max_length=80)


class MemoryEpisodeSummary(BaseModel):
    """任务回顾的可检索摘要与验证边界。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    title: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=800)
    outcome: Literal["completed", "partial", "failed", "cancelled", "unknown"]
    result: str = Field(min_length=1, max_length=1500)
    attempts: str = Field(default="", max_length=1000)
    verification_scope: str = Field(default="", max_length=500)
    reusable_learnings: tuple[Annotated[str, Field(max_length=200)], ...] = Field(
        default=(), max_length=8
    )
    unresolved: tuple[Annotated[str, Field(max_length=200)], ...] = Field(
        default=(), max_length=8
    )


class MemoryEpisodeProposal(BaseModel):
    """模型提交的任务回顾及其允许引用的真实来源 ID。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    retain: bool
    reason_code: str | None = Field(default=None, max_length=100)
    summary: MemoryEpisodeSummary | None = None
    source_ref_ids: tuple[str, ...] = Field(max_length=80)

    @model_validator(mode="after")
    def validate_retained_episode(self) -> "MemoryEpisodeProposal":
        """只保留同时具有摘要和来源的回顾。"""
        if self.retain and (self.summary is None or not self.source_ref_ids):
            raise ValueError("memory_episode_sources_required")
        if not self.retain and (self.summary is not None or self.source_ref_ids):
            raise ValueError("memory_episode_discard_payload_invalid")
        if len(set(self.source_ref_ids)) != len(self.source_ref_ids):
            raise ValueError("memory_episode_duplicate_source_ref")
        return self


class MemoryEpisode(BaseModel):
    """持久化任务回顾，来源只能由程序从冻结证据中解析。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    episode_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    project_id: str | None = Field(default=None, min_length=1)
    content: str = Field(min_length=1, max_length=8000)
    summary: MemoryEpisodeSummary
    source_refs: tuple[MemorySourceReference, ...] = Field(min_length=1, max_length=80)
    source_fingerprint: str = Field(min_length=64, max_length=64)
    created_at: str


class MemoryFactProposal(BaseModel):
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
    confidence: float = Field(ge=0.0, le=1.0)
    polarity: Literal["affirmed", "negated"] = "affirmed"
    claim_intent: Literal["assert", "correct", "retract"] = "assert"

    @model_validator(mode="after")
    def validate_retraction_value(self) -> "MemoryFactProposal":
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


class MemoryFactExtractionOutput(BaseModel):
    """严格校验的大模型事实提取结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    atomic_facts: tuple[MemoryFactProposal, ...] = Field(max_length=20)


class MemoryFactExtractionSnapshot(BaseModel):
    """一次轮次的冻结提取清单；空列表也表示已成功提取。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    turn_id: str = Field(min_length=1)
    candidates: tuple["MemoryCandidateSnapshot", ...]


class MemoryCandidateSnapshot(BaseModel):
    """可稳定写入作业快照的单条候选。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    content: str = Field(min_length=1, max_length=600)
    source_turn_id: str = Field(min_length=1)
    memory_type: str = "fact"
    category: str = "fact"
    confidence: float = Field(ge=0.6, le=1.0)
    session_id: str = ""
    fact: MemoryFact | None = None

    @classmethod
    def from_candidate(cls, candidate: MemoryCandidate) -> "MemoryCandidateSnapshot":
        """将领域候选转为可持久化的严格快照。"""
        return cls(
            content=candidate.content,
            source_turn_id=candidate.source_turn_id,
            memory_type=candidate.memory_type,
            category=candidate.category,
            confidence=candidate.confidence,
            session_id=candidate.session_id,
            fact=candidate.fact,
        )

    def to_candidate(self) -> MemoryCandidate:
        """恢复冻结快照对应的领域候选。"""
        from .entities import MemoryCandidate

        return MemoryCandidate(
            content=self.content,
            source_turn_id=self.source_turn_id,
            memory_type=self.memory_type,
            category=self.category,
            confidence=self.confidence,
            session_id=self.session_id,
            fact=self.fact,
        )


MemoryFactExtractionSnapshot.model_rebuild()


class MemoryResolutionProposal(BaseModel):
    """模型针对一个候选提出的动作；目标 ID 必须由程序复核。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    action: Literal["CREATE", "REVISE", "IGNORE", "DEFER"]
    target_memory_id: str | None = None
    relation: Literal["supersedes", "contradicts"] | None = None
    reason_code: str | None = Field(default=None, max_length=80)
    expected_memory_ids: tuple[str, ...] = ()


class MemoryWriteOperation(BaseModel):
    """单个候选的可恢复处理回执。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    candidate: MemoryCandidateSnapshot
    status: Literal[
        "pending", "applied", "ignored", "deferred", "rejected", "skipped"
    ]
    decision: MemoryResolutionProposal | None = None
    result_memory_id: str | None = None
    projection_status: Literal["not_required", "pending", "ready", "failed"]


class MemoryRecordMetadata(BaseModel):
    """新记忆记录的稳定字段与开放扩展字段。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    fact: MemoryFact | None = None


__all__ = [
    "MemoryCandidateSnapshot",
    "MemoryEpisode",
    "MemoryEpisodeProposal",
    "MemoryEpisodeSummary",
    "MemoryExecutionEvidence",
    "MemoryFact",
    "MemoryFactIdentity",
    "MemoryFactExtractionOutput",
    "MemoryFactExtractionSnapshot",
    "MemoryFactProposal",
    "MemoryRecordMetadata",
    "MemoryResolutionProposal",
    "MemorySourceReference",
    "MemoryWriteOperation",
]
