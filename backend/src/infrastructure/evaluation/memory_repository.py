"""In-memory evaluation repository for tests and local CLI runs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from domain.evaluation import CaseResult, DatasetManifest, EvalRun, GateResult, MetricValue


class InMemoryEvaluationRepository:
    def __init__(self) -> None:
        self.manifests: dict[str, DatasetManifest] = {}
        self.runs: dict[str, EvalRun] = {}
        self.results: dict[tuple[str, str], CaseResult] = {}
        self.attempts: list[CaseResult] = []
        self.metrics: list[MetricValue] = []
        self.gates: list[GateResult] = []

    async def create_manifest(self, manifest: DatasetManifest) -> None:
        self.manifests[manifest.version_id] = manifest

    async def get_manifest(self, version_id: str) -> DatasetManifest | None:
        return self.manifests.get(version_id)

    async def create_run(self, run: EvalRun) -> None:
        if run.run_id in self.runs:
            raise ValueError(f"evaluation run already exists: {run.run_id}")
        self.runs[run.run_id] = run

    async def update_run(self, run: EvalRun) -> None:
        self.runs[run.run_id] = run

    async def cancel_run(self, run_id: str) -> EvalRun | None:
        from dataclasses import replace
        run = self.runs.get(run_id)
        if run is None:
            return None
        value = replace(
            run,
            status="cancelled",
            completed_at=datetime.now(timezone.utc),
        )
        self.runs[run_id] = value
        return value

    async def save_case_result(self, result: CaseResult) -> None:
        self.attempts.append(result)
        self.results[(result.run_id, result.case_id)] = result

    async def save_metric(self, metric: MetricValue) -> None:
        self.metrics = [item for item in self.metrics if not (item.run_id == metric.run_id and item.case_id == metric.case_id and item.metric_name == metric.metric_name)]
        self.metrics.append(metric)

    async def save_gate(self, gate: GateResult) -> None:
        self.gates = [item for item in self.gates if not (item.run_id == gate.run_id and item.metric == gate.metric)]
        self.gates.append(gate)

    async def get_run(self, run_id: str) -> EvalRun | None:
        return self.runs.get(run_id)

    async def list_runs(self, *, limit: int = 50, offset: int = 0, status: str | None = None, dataset_version: str | None = None):
        values = [run for run in self.runs.values() if (status is None or str(run.status) == status) and (dataset_version is None or run.dataset_version == dataset_version)]
        values.sort(key=lambda run: str(run.started_at or ""), reverse=True)
        return tuple(values[offset:offset + limit]), len(values)

    async def list_case_results(self, run_id: str) -> Sequence[CaseResult]:
        return tuple(result for (stored_run_id, _), result in sorted(self.results.items()) if stored_run_id == run_id)

    async def list_metrics(self, run_id: str, *, metric_name: str | None = None) -> Sequence[MetricValue]:
        return tuple(sorted((metric for metric in self.metrics if metric.run_id == run_id and (metric_name is None or metric.metric_name == metric_name)), key=lambda item: (item.metric_name, item.case_id or "")))

    async def list_gates(self, run_id: str) -> Sequence[GateResult]:
        return tuple(gate for gate in self.gates if gate.run_id == run_id)

    async def list_manifests(self) -> Sequence[DatasetManifest]:
        return tuple(self.manifests.values())


__all__ = ["InMemoryEvaluationRepository"]
