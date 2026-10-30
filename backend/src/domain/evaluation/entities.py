"""Framework-independent evaluation entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping


class EvaluationMode(StrEnum):
    RETRIEVAL = "retrieval"
    CHAT = "chat"
    AGENT = "agent"


class EvaluationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RelevanceLabel:
    source_id: str
    grade: int = 2

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ValueError("source_id must not be empty")
        if self.grade not in (0, 1, 2):
            raise ValueError("relevance grade must be 0, 1 or 2")


@dataclass(frozen=True, slots=True)
class RetrievalExpectation:
    scope: str = "knowledge"
    relevant_sources: tuple[RelevanceLabel, ...] = ()

    @property
    def relevant_source_ids(self) -> frozenset[str]:
        return frozenset(item.source_id for item in self.relevant_sources if item.grade > 0)


@dataclass(frozen=True, slots=True)
class AnswerExpectation:
    reference: str | None = None
    must_include: tuple[str, ...] = ()
    must_not_include: tuple[str, ...] = ()
    json_schema: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AgentExpectation:
    allowed_tools: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    expected_effects: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str
    task_type: str
    input: Mapping[str, Any]
    retrieval: RetrievalExpectation | None = None
    answer: AnswerExpectation | None = None
    agent: AgentExpectation | None = None
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    dataset_name: str
    version_id: str
    manifest_sha256: str
    schema_version: int
    case_count: int
    tag_counts: Mapping[str, int]
    cases: tuple[EvalCase, ...]
    corpus: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GateRule:
    metric: str
    op: str
    value: float
    required: bool = True

    def __post_init__(self) -> None:
        if self.op not in {">=", "<=", "==", ">", "<"}:
            raise ValueError(f"unsupported gate operator: {self.op}")


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    name: str
    dataset_version: str
    mode: EvaluationMode
    target: Mapping[str, Any]
    evaluators: tuple[str, ...]
    gates: tuple[GateRule, ...] = ()
    concurrency: int = 1
    timeout_seconds: float = 60.0
    max_attempts: int = 1
    baseline_run_id: str | None = None

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")


@dataclass(frozen=True, slots=True)
class ExecutionArtifact:
    output_text: str = ""
    ranking: tuple[str, ...] = ()
    retrieval_run_id: str | None = None
    agent_run_id: str | None = None
    trace: Mapping[str, Any] = field(default_factory=dict)
    duration_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost: float | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class MetricValue:
    run_id: str
    metric_name: str
    value: float | None
    case_id: str | None = None
    value_json: Mapping[str, Any] = field(default_factory=dict)
    evaluator_version: str = "1"
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CaseAttempt:
    attempt_id: str
    run_id: str
    case_id: str
    attempt_number: int
    status: str
    artifact: ExecutionArtifact
    created_at: datetime | None = None
    input_snapshot: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CaseResult:
    run_id: str
    case_id: str
    status: str
    attempt: CaseAttempt
    metrics: tuple[MetricValue, ...] = ()
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class EvalRun:
    run_id: str
    dataset_version: str
    mode: EvaluationMode | str
    status: EvaluationStatus | str
    name: str
    config: Mapping[str, Any] = field(default_factory=dict)
    git_sha: str | None = None
    baseline_run_id: str | None = None
    case_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class GateResult:
    run_id: str
    metric: str
    status: str
    actual: float | None
    rule: GateRule
    baseline: float | None = None
    reason: str | None = None

