"""Application contracts for evaluation targets, metrics, and progress."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from domain.evaluation import (
    EvalCase,
    EvaluationConfig,
    ExecutionArtifact,
    MetricValue,
)


class EvaluationTarget(Protocol):
    async def execute(
        self, case: EvalCase, config: EvaluationConfig
    ) -> ExecutionArtifact: ...


class MetricEvaluator(Protocol):
    name: str
    version: str

    async def evaluate(
        self,
        case: EvalCase,
        artifact: ExecutionArtifact,
        *,
        run_id: str,
    ) -> Sequence[MetricValue]: ...


class EvaluationEventSink(Protocol):
    async def publish(self, event_type: str, payload: dict[str, Any]) -> None: ...


__all__ = ["EvaluationEventSink", "EvaluationTarget", "MetricEvaluator"]
