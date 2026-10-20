"""Ports used by the evaluation application service."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from .entities import (
    CaseResult,
    DatasetManifest,
    EvalCase,
    EvalRun,
    EvaluationConfig,
    ExecutionArtifact,
    GateResult,
    MetricValue,
)


class EvaluationRepository(Protocol):
    async def create_manifest(self, manifest: DatasetManifest) -> None: ...
    async def get_manifest(self, version_id: str) -> DatasetManifest | None: ...
    async def create_run(self, run: EvalRun) -> None: ...
    async def update_run(self, run: EvalRun) -> None: ...
    async def cancel_run(self, run_id: str) -> EvalRun | None: ...
    async def save_case_result(self, result: CaseResult) -> None: ...
    async def save_metric(self, metric: MetricValue) -> None: ...
    async def save_gate(self, gate: GateResult) -> None: ...
    async def get_run(self, run_id: str) -> EvalRun | None: ...
    async def list_case_results(self, run_id: str) -> Sequence[CaseResult]: ...
    async def list_metrics(self, run_id: str, *, metric_name: str | None = None) -> Sequence[MetricValue]: ...
    async def list_gates(self, run_id: str) -> Sequence[GateResult]: ...


class EvaluationTarget(Protocol):
    async def execute(self, case: EvalCase, config: EvaluationConfig) -> ExecutionArtifact: ...


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

