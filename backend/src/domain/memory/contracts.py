"""记忆事实、候选快照与解析结果的领域类型。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .entities import MemoryCandidate


_FACT_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def _required_text(value: str, field_name: str, maximum: int | None = None) -> None:
    """校验领域文本字段的非空条件与最大长度。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}_required")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{field_name}_too_long")


@dataclass(frozen=True, slots=True)
class MemorySourceReference:
    """指向原始对话或执行证据的引用。"""

    source_type: Literal["turn", "run", "worker_message", "artifact"]
    source_id: str
    excerpt: str
    observed_at: str

    def __post_init__(self) -> None:
        """校验引用来源和可追溯正文。"""
        if self.source_type not in {"turn", "run", "worker_message", "artifact"}:
            raise ValueError("memory_source_type_invalid")
        _required_text(self.source_id, "memory_source_id")
        _required_text(self.excerpt, "memory_source_excerpt", 2000)
        if not isinstance(self.observed_at, str):
            raise ValueError("memory_source_observed_at_invalid")


@dataclass(frozen=True, slots=True)
class MemoryFact:
    """已提取事实的主体、适用范围、值与来源。"""

    scope_kind: Literal["global", "project", "session"]
    scope_id: str
    subject_key: str
    fact_key: str
    fact_value: str | None
    confidence: float
    asserted_at: str
    source_refs: tuple[MemorySourceReference, ...]
    retracts_value: str | None = None
    polarity: Literal["affirmed", "negated"] = "affirmed"
    claim_intent: Literal["assert", "correct", "retract"] = "assert"
    valid_from: str | None = None
    valid_until: str | None = None

    def __post_init__(self) -> None:
        """校验事实范围、可信度、来源和撤回规则。"""
        if self.scope_kind not in {"global", "project", "session"}:
            raise ValueError("memory_fact_scope_kind_invalid")
        _required_text(self.scope_id, "memory_fact_scope_id")
        _required_text(self.subject_key, "memory_fact_subject_key", 100)
        _required_text(self.fact_key, "memory_fact_key", 30)
        if not _FACT_KEY_PATTERN.fullmatch(self.fact_key):
            raise ValueError("memory_fact_key_invalid")
        if self.fact_value is not None:
            _required_text(self.fact_value, "memory_fact_value", 500)
        if self.retracts_value is not None:
            _required_text(self.retracts_value, "memory_fact_retracts_value", 500)
        if not 0.6 <= self.confidence <= 1.0:
            raise ValueError("memory_fact_confidence_invalid")
        if self.claim_intent not in {"assert", "correct", "retract"}:
            raise ValueError("memory_fact_claim_intent_invalid")
        if self.polarity not in {"affirmed", "negated"}:
            raise ValueError("memory_fact_polarity_invalid")
        if not isinstance(self.asserted_at, str):
            raise ValueError("memory_fact_asserted_at_invalid")
        if not self.source_refs or any(
            not isinstance(reference, MemorySourceReference)
            for reference in self.source_refs
        ):
            raise ValueError("memory_fact_source_refs_required")
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


@dataclass(frozen=True, slots=True)
class MemoryFactIdentity:
    """用于精确复核事实组的稳定身份。"""

    scope_kind: Literal["global", "project", "session"]
    scope_id: str
    subject_key: str
    fact_key: str

    def __post_init__(self) -> None:
        """校验事实组身份字段。"""
        if self.scope_kind not in {"global", "project", "session"}:
            raise ValueError("memory_fact_scope_kind_invalid")
        _required_text(self.scope_id, "memory_fact_scope_id")
        _required_text(self.subject_key, "memory_fact_subject_key", 100)
        _required_text(self.fact_key, "memory_fact_key", 30)
        if not _FACT_KEY_PATTERN.fullmatch(self.fact_key):
            raise ValueError("memory_fact_key_invalid")


@dataclass(frozen=True, slots=True)
class MemoryToolOutcomeEvidence:
    """Worker 工具回执的最小、可追溯结果。"""

    message_id: str
    tool_name: str
    status: str
    observed_at: str
    result_ref: str | None = None

    def __post_init__(self) -> None:
        """校验工具回执的身份和状态文本。"""
        _required_text(self.message_id, "memory_tool_message_id")
        _required_text(self.tool_name, "memory_tool_name", 120)
        _required_text(self.status, "memory_tool_status", 40)
        if not isinstance(self.observed_at, str):
            raise ValueError("memory_tool_observed_at_invalid")


@dataclass(frozen=True, slots=True)
class MemoryTaskOutcomeEvidence:
    """一次任务最终状态及其有限的工具证据。"""

    task_id: str
    title: str
    status: str
    execution_generation: int
    tool_outcomes: tuple[MemoryToolOutcomeEvidence, ...] = ()
    error_code: str | None = None
    result_ref: str | None = None

    def __post_init__(self) -> None:
        """校验任务身份、代次和有限工具证据。"""
        _required_text(self.task_id, "memory_task_id")
        _required_text(self.title, "memory_task_title", 300)
        _required_text(self.status, "memory_task_status", 40)
        if type(self.execution_generation) is not int or self.execution_generation < 0:
            raise ValueError("memory_task_execution_generation_invalid")
        if self.error_code is not None and len(self.error_code) > 100:
            raise ValueError("memory_task_error_code_too_long")
        if len(self.tool_outcomes) > 4 or any(
            not isinstance(outcome, MemoryToolOutcomeEvidence)
            for outcome in self.tool_outcomes
        ):
            raise ValueError("memory_task_tool_outcomes_invalid")


@dataclass(frozen=True, slots=True)
class MemoryExecutionEvidence:
    """Run 的冻结执行摘要，只存状态和来源引用，不复制完整工具输出。"""

    run_id: str
    source_fingerprint: str
    total_task_count: int
    truncated: bool
    tasks: tuple[MemoryTaskOutcomeEvidence, ...] = ()
    source_refs: tuple[MemorySourceReference, ...] = ()

    def __post_init__(self) -> None:
        """校验冻结证据的 Run 身份、摘要指纹和列表上限。"""
        _required_text(self.run_id, "memory_execution_run_id")
        if len(self.source_fingerprint) != 64:
            raise ValueError("memory_execution_fingerprint_invalid")
        if type(self.total_task_count) is not int or self.total_task_count < 0:
            raise ValueError("memory_execution_task_count_invalid")
        if len(self.tasks) > 12 or any(
            not isinstance(task, MemoryTaskOutcomeEvidence) for task in self.tasks
        ):
            raise ValueError("memory_execution_tasks_invalid")
        if len(self.source_refs) > 80 or any(
            not isinstance(reference, MemorySourceReference)
            for reference in self.source_refs
        ):
            raise ValueError("memory_execution_source_refs_invalid")


@dataclass(frozen=True, slots=True)
class MemoryEpisodeSummary:
    """任务回顾的可检索摘要与验证边界。"""

    title: str
    goal: str
    outcome: Literal["completed", "partial", "failed", "cancelled", "unknown"]
    result: str
    attempts: str = ""
    verification_scope: str = ""
    reusable_learnings: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """校验摘要字段长度及可复用经验列表。"""
        _required_text(self.title, "memory_episode_title", 160)
        _required_text(self.goal, "memory_episode_goal", 800)
        if self.outcome not in {"completed", "partial", "failed", "cancelled", "unknown"}:
            raise ValueError("memory_episode_outcome_invalid")
        _required_text(self.result, "memory_episode_result", 1500)
        if len(self.attempts) > 1000 or len(self.verification_scope) > 500:
            raise ValueError("memory_episode_summary_field_too_long")
        if len(self.reusable_learnings) > 8 or any(
            not isinstance(item, str) or len(item) > 200
            for item in self.reusable_learnings
        ):
            raise ValueError("memory_episode_learnings_invalid")
        if len(self.unresolved) > 8 or any(
            not isinstance(item, str) or len(item) > 200 for item in self.unresolved
        ):
            raise ValueError("memory_episode_unresolved_invalid")


@dataclass(frozen=True, slots=True)
class MemoryEpisodeProposal:
    """等待程序校验来源后保存的任务回顾提案。"""

    retain: bool
    source_ref_ids: tuple[str, ...] = ()
    reason_code: str | None = None
    summary: MemoryEpisodeSummary | None = None

    def __post_init__(self) -> None:
        """确保保留的回顾具备摘要和唯一来源，不保留时不带正文。"""
        if self.retain and (self.summary is None or not self.source_ref_ids):
            raise ValueError("memory_episode_sources_required")
        if not self.retain and (self.summary is not None or self.source_ref_ids):
            raise ValueError("memory_episode_discard_payload_invalid")
        if len(set(self.source_ref_ids)) != len(self.source_ref_ids):
            raise ValueError("memory_episode_duplicate_source_ref")


@dataclass(frozen=True, slots=True)
class MemoryEpisode:
    """持久化任务回顾，来源只能由程序从冻结证据中解析。"""

    episode_id: str
    turn_id: str
    session_id: str
    content: str
    summary: MemoryEpisodeSummary
    source_refs: tuple[MemorySourceReference, ...]
    source_fingerprint: str
    created_at: str
    project_id: str | None = None

    def __post_init__(self) -> None:
        """校验回顾身份、正文和可追溯来源。"""
        for value, name in (
            (self.episode_id, "memory_episode_id"),
            (self.turn_id, "memory_episode_turn_id"),
            (self.session_id, "memory_episode_session_id"),
        ):
            _required_text(value, name)
        if self.project_id is not None:
            _required_text(self.project_id, "memory_episode_project_id")
        _required_text(self.content, "memory_episode_content", 8000)
        if not isinstance(self.summary, MemoryEpisodeSummary):
            raise ValueError("memory_episode_summary_invalid")
        if not 1 <= len(self.source_refs) <= 80 or any(
            not isinstance(reference, MemorySourceReference)
            for reference in self.source_refs
        ):
            raise ValueError("memory_episode_source_refs_invalid")
        if len(self.source_fingerprint) != 64:
            raise ValueError("memory_episode_fingerprint_invalid")


@dataclass(frozen=True, slots=True)
class MemoryFactExtractionSnapshot:
    """一次轮次的冻结提取清单；空列表也表示已成功提取。"""

    turn_id: str
    candidates: tuple[MemoryCandidateSnapshot, ...]
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        """校验冻结清单版本和轮次身份。"""
        _required_text(self.turn_id, "memory_snapshot_turn_id")
        if self.schema_version != 1:
            raise ValueError("memory_snapshot_schema_version_invalid")
        if any(not isinstance(candidate, MemoryCandidateSnapshot) for candidate in self.candidates):
            raise ValueError("memory_snapshot_candidates_invalid")


@dataclass(frozen=True, slots=True)
class MemoryCandidateSnapshot:
    """可稳定写入作业快照的单条候选。"""

    content: str
    source_turn_id: str
    confidence: float
    memory_type: str = "fact"
    category: str = "fact"
    session_id: str = ""
    fact: MemoryFact | None = None

    def __post_init__(self) -> None:
        """校验候选正文、来源轮次和事实阈值。"""
        _required_text(self.content, "memory_candidate_content", 600)
        _required_text(self.source_turn_id, "memory_candidate_source_turn_id")
        if not 0.6 <= self.confidence <= 1.0:
            raise ValueError("memory_candidate_confidence_invalid")
        if self.fact is not None and not isinstance(self.fact, MemoryFact):
            raise ValueError("memory_candidate_fact_invalid")

    @classmethod
    def from_candidate(cls, candidate: MemoryCandidate) -> MemoryCandidateSnapshot:
        """将领域候选复制为可恢复的快照类型。"""
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
        """根据冻结快照恢复领域候选。"""
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


@dataclass(frozen=True, slots=True)
class MemoryResolutionProposal:
    """模型针对一个候选提出的动作；目标 ID 必须由程序复核。"""

    action: Literal["CREATE", "REVISE", "IGNORE", "DEFER"]
    target_memory_id: str | None = None
    relation: Literal["supersedes", "contradicts"] | None = None
    reason_code: str | None = None
    expected_memory_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """校验决策动作及可选关系类型。"""
        if self.action not in {"CREATE", "REVISE", "IGNORE", "DEFER"}:
            raise ValueError("memory_resolution_action_invalid")
        if self.relation not in {None, "supersedes", "contradicts"}:
            raise ValueError("memory_resolution_relation_invalid")


@dataclass(frozen=True, slots=True)
class MemoryWriteOperation:
    """单个候选的可恢复处理回执。"""

    operation_id: str
    turn_id: str
    candidate: MemoryCandidateSnapshot
    status: Literal["pending", "applied", "ignored", "deferred", "rejected", "skipped"]
    projection_status: Literal["not_required", "pending", "ready", "failed"]
    decision: MemoryResolutionProposal | None = None
    result_memory_id: str | None = None

    def __post_init__(self) -> None:
        """校验写入回执身份和状态组合。"""
        _required_text(self.operation_id, "memory_operation_id")
        _required_text(self.turn_id, "memory_operation_turn_id")
        if not isinstance(self.candidate, MemoryCandidateSnapshot):
            raise ValueError("memory_operation_candidate_invalid")
        if self.status not in {
            "pending", "applied", "ignored", "deferred", "rejected", "skipped"
        }:
            raise ValueError("memory_operation_status_invalid")
        if self.projection_status not in {"not_required", "pending", "ready", "failed"}:
            raise ValueError("memory_operation_projection_status_invalid")


@dataclass(frozen=True, slots=True)
class MemoryRecordMetadata:
    """记忆事实及其他稳定写入属性组成的领域元数据。"""

    fact: MemoryFact | None = None
    extra_fields: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """确保结构化事实和开放扩展字段采用已解析类型。"""
        if self.fact is not None and not isinstance(self.fact, MemoryFact):
            raise ValueError("memory_record_metadata_fact_invalid")
        if not isinstance(self.extra_fields, dict):
            raise ValueError("memory_record_metadata_extra_fields_invalid")


__all__ = [
    "MemoryCandidateSnapshot",
    "MemoryEpisode",
    "MemoryEpisodeProposal",
    "MemoryEpisodeSummary",
    "MemoryExecutionEvidence",
    "MemoryFact",
    "MemoryFactIdentity",
    "MemoryFactExtractionSnapshot",
    "MemoryRecordMetadata",
    "MemoryResolutionProposal",
    "MemorySourceReference",
    "MemoryTaskOutcomeEvidence",
    "MemoryToolOutcomeEvidence",
    "MemoryWriteOperation",
]
