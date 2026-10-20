"""Read-only evaluation query service."""

from __future__ import annotations

from typing import Any

from domain.evaluation.ports import EvaluationRepository
from domain.evaluation import GateRule
from .gates import evaluate_gates


class EvaluationQueryService:
    def __init__(self, repository: EvaluationRepository) -> None:
        self.repository = repository

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = await self.repository.get_run(run_id)
        if run is None:
            return None
        metrics = await self.repository.list_metrics(run_id)
        gates = await self.repository.list_gates(run_id) if hasattr(self.repository, "list_gates") else ()
        results = await self.repository.list_case_results(run_id)
        metrics_by_case: dict[str, list[dict[str, Any]]] = {}
        for metric in metrics:
            if metric.case_id is not None:
                metrics_by_case.setdefault(metric.case_id, []).append(self._metric(metric))
        cases = [self._case(item) for item in results]
        for item in cases:
            item["metrics"] = metrics_by_case.get(item["case_id"], item["metrics"])
        return {
            "run_id": run.run_id,
            "dataset_version": run.dataset_version,
            "mode": str(run.mode),
            "status": str(run.status),
            "name": run.name,
            "config": dict(run.config),
            "case_count": run.case_count,
            "completed_count": run.completed_count,
            "failed_count": run.failed_count,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "metrics": [self._metric(item) for item in metrics],
            "gates": [self._gate(item) for item in gates],
            "cases": cases,
        }

    async def list_runs(self, **filters: Any) -> tuple[list[dict[str, Any]], int]:
        values, total = await self.repository.list_runs(**filters)
        return [self._run(item) for item in values], total

    async def list_datasets(self) -> list[dict[str, Any]]:
        values = await self.repository.list_manifests()
        return [{"dataset_name": item.dataset_name, "version_id": item.version_id, "manifest_sha256": item.manifest_sha256, "case_count": item.case_count, "tag_counts": dict(item.tag_counts)} for item in values]

    async def compare(self, run_id: str, baseline_run_id: str) -> dict[str, Any] | None:
        current = await self.repository.list_metrics(run_id)
        baseline = await self.repository.list_metrics(baseline_run_id)
        if await self.repository.get_run(run_id) is None or await self.repository.get_run(baseline_run_id) is None:
            return None
        current_values = {item.metric_name: item.value for item in current if item.case_id is None}
        baseline_values = {item.metric_name: item.value for item in baseline if item.case_id is None}
        names = sorted(set(current_values) | set(baseline_values))
        return {"run_id": run_id, "baseline_run_id": baseline_run_id, "items": [{"metric_name": name, "value": current_values.get(name), "baseline": baseline_values.get(name), "delta": None if current_values.get(name) is None or baseline_values.get(name) is None else current_values[name] - baseline_values[name]} for name in names]}

    async def evaluate_gates(self, run_id: str, rules: tuple[GateRule, ...]) -> list[dict[str, Any]] | None:
        run = await self.repository.get_run(run_id)
        if run is None:
            return None
        metrics = await self.repository.list_metrics(run_id)
        baseline_values: dict[str, float] = {}
        if run.baseline_run_id:
            baseline = await self.repository.list_metrics(run.baseline_run_id)
            baseline_values = {item.metric_name: float(item.value) for item in baseline if item.case_id is None and item.value is not None}
        results = evaluate_gates(run_id, rules, metrics, baseline=baseline_values)
        for item in results:
            await self.repository.save_gate(item)
        return [self._gate(item) for item in results]

    @staticmethod
    def _run(run) -> dict[str, Any]:
        return {"run_id": run.run_id, "dataset_version": run.dataset_version, "mode": str(run.mode), "status": str(run.status), "name": run.name, "case_count": run.case_count, "completed_count": run.completed_count, "failed_count": run.failed_count, "started_at": run.started_at, "completed_at": run.completed_at}

    @staticmethod
    def _metric(item) -> dict[str, Any]:
        return {"metric_id": item.metric_name, "metric_name": item.metric_name, "case_id": item.case_id, "value": item.value, "value_json": dict(item.value_json), "evidence": list(item.evidence)}

    @staticmethod
    def _gate(item) -> dict[str, Any]:
        return {"metric": item.metric, "status": item.status, "actual": item.actual, "baseline": item.baseline, "op": item.rule.op, "threshold": item.rule.value, "reason": item.reason}

    @staticmethod
    def _case(item) -> dict[str, Any]:
        return {"case_id": item.case_id, "status": item.status, "attempt": item.attempt.attempt_number, "error": item.error_message, "output": item.attempt.artifact.output_text, "ranking": list(item.attempt.artifact.ranking), "retrieval_run_id": item.attempt.artifact.retrieval_run_id, "agent_run_id": item.attempt.artifact.agent_run_id, "duration_ms": item.attempt.artifact.duration_ms, "metrics": [EvaluationQueryService._metric(metric) for metric in item.metrics]}


__all__ = ["EvaluationQueryService"]
