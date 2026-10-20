"""Evaluation domain contracts and deterministic metrics."""

from .entities import (
    AgentExpectation,
    AnswerExpectation,
    CaseAttempt,
    CaseResult,
    DatasetManifest,
    EvalCase,
    EvalRun,
    EvaluationConfig,
    EvaluationMode,
    EvaluationStatus,
    ExecutionArtifact,
    GateResult,
    GateRule,
    MetricValue,
    RelevanceLabel,
    RetrievalExpectation,
)
from .metrics import evaluate_retrieval_case, aggregate_metrics

__all__ = [
    "AgentExpectation", "AnswerExpectation", "CaseAttempt", "CaseResult",
    "DatasetManifest", "EvalCase", "EvalRun", "EvaluationConfig",
    "EvaluationMode", "EvaluationStatus", "ExecutionArtifact", "GateResult",
    "GateRule", "MetricValue", "RelevanceLabel", "RetrievalExpectation",
    "evaluate_retrieval_case", "aggregate_metrics",
]
