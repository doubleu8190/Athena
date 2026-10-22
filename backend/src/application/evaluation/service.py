"""Evaluation run orchestration shared by CLI and HTTP adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from domain.evaluation import (
    CaseAttempt,
    CaseResult,
    DatasetManifest,
    EvalRun,
    EvaluationConfig,
    EvaluationStatus,
    GateResult,
    MetricValue,
    ExecutionArtifact,
)
from domain.evaluation.ports import EvaluationRepository
from shared import new_id

from .evaluators import default_evaluators
from .gates import evaluate_gates
from .ports import EvaluationEventSink, EvaluationTarget, MetricEvaluator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EvaluationService:
    def __init__(
        self,
        repository: EvaluationRepository,
        *,
        target: EvaluationTarget,
        evaluators: Mapping[str, MetricEvaluator] | None = None,
        event_sink: EvaluationEventSink | None = None,
    ) -> None:
        self.repository = repository
        self.target = target
        self.evaluators = dict(evaluators or default_evaluators())
        self.event_sink = event_sink

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_sink is not None:
            try:
                await self.event_sink.publish(event_type, payload)
            except Exception:
                # Progress delivery is advisory; a disconnected event store
                # must not turn a completed evaluation into a failed run.
                return

    def build_run(self, manifest: DatasetManifest, config: EvaluationConfig, *, run_id: str | None = None, status: EvaluationStatus = EvaluationStatus.QUEUED) -> EvalRun:
        return EvalRun(
            run_id=run_id or new_id("eval"),
            dataset_version=manifest.version_id,
            mode=config.mode,
            status=status,
            name=config.name,
            config={"target": dict(config.target), "evaluators": list(config.evaluators), "gates": [{"metric": rule.metric, "op": rule.op, "value": rule.value, "required": rule.required} for rule in config.gates]},
            baseline_run_id=config.baseline_run_id,
            case_count=manifest.case_count,
        )

    async def queue(self, manifest: DatasetManifest, config: EvaluationConfig, *, run_id: str | None = None) -> EvalRun:
        run = self.build_run(manifest, config, run_id=run_id)
        await self.repository.create_run(run)
        await self._publish("evaluation.started", {"evaluation_run_id": run.run_id, "case_count": manifest.case_count, "status": "queued"})
        return run

    async def run(self, manifest: DatasetManifest, config: EvaluationConfig, *, run_id: str | None = None) -> EvalRun:
        run_id = run_id or new_id("eval")
        existing = await self.repository.get_run(run_id)
        run = replace(self.build_run(manifest, config, run_id=run_id, status=EvaluationStatus.RUNNING), started_at=_now())
        if existing is None:
            await self.repository.create_run(run)
        else:
            await self.repository.update_run(run)
        await self._publish("evaluation.started", {"evaluation_run_id": run_id, "case_count": manifest.case_count})
        semaphore = asyncio.Semaphore(config.concurrency)
        completed = 0
        failed = 0
        results: list[CaseResult] = []

        async def execute_case(case) -> CaseResult:
            nonlocal completed, failed
            async with semaphore:
                current_run = await self.repository.get_run(run_id)
                if current_run is not None and str(current_run.status) == EvaluationStatus.CANCELLED:
                    cancelled_attempt = CaseAttempt(new_id("attempt"), run_id, case.case_id, 0, "cancelled", ExecutionArtifact(error_message="cancelled"), _now(), dict(case.input))
                    result = CaseResult(run_id, case.case_id, "cancelled", cancelled_attempt, (), "cancelled")
                    await self.repository.save_case_result(result)
                    return result
                await self._publish("evaluation.case_started", {"evaluation_run_id": run_id, "case_id": case.case_id})
                last_artifact = None
                last_error = None
                attempt_number = 0
                for attempt_number in range(1, config.max_attempts + 1):
                    try:
                        last_artifact = await asyncio.wait_for(self.target.execute(case, config), config.timeout_seconds)
                        last_error = None
                        break
                    except asyncio.TimeoutError:
                        last_artifact = ExecutionArtifact(error_message="timeout")
                        last_error = "timeout"
                    except Exception as exc:  # target failures belong to the case result
                        last_artifact = ExecutionArtifact(error_message=str(exc))
                        last_error = str(exc)
                    if attempt_number < config.max_attempts:
                        failed_attempt = CaseAttempt(new_id("attempt"), run_id, case.case_id, attempt_number, "retrying", last_artifact, _now(), dict(case.input))
                        await self.repository.save_case_result(CaseResult(run_id, case.case_id, "retrying", failed_attempt, (), last_error))
                assert last_artifact is not None
                attempt = CaseAttempt(new_id("attempt"), run_id, case.case_id, attempt_number, "failed" if last_error else "completed", last_artifact, _now(), dict(case.input))
                metrics: list[MetricValue] = []
                if not last_error:
                    for evaluator_name in config.evaluators:
                        evaluator = self.evaluators.get(evaluator_name)
                        if evaluator is None:
                            last_error = f"unknown evaluator: {evaluator_name}"
                            break
                        try:
                            metrics.extend(await evaluator.evaluate(case, last_artifact, run_id=run_id))
                        except Exception as exc:
                            last_error = f"evaluator {evaluator_name} failed: {exc}"
                            break
                status = "failed" if last_error else "completed"
                result = CaseResult(run_id, case.case_id, status, attempt, tuple(metrics), last_error)
                await self.repository.save_case_result(result)
                for metric in metrics:
                    await self.repository.save_metric(metric)
                completed += 1
                failed += int(bool(last_error))
                await self._publish("evaluation.case_failed" if last_error else "evaluation.case_completed", {"evaluation_run_id": run_id, "case_id": case.case_id, "completed_count": completed, "case_count": manifest.case_count, "error": last_error})
                return result

        results = list(await asyncio.gather(*(execute_case(case) for case in manifest.cases)))
        current_run = await self.repository.get_run(run_id)
        if current_run is not None and str(current_run.status) == EvaluationStatus.CANCELLED:
            return current_run
        all_metrics = [metric for result in results for metric in result.metrics]
        from domain.evaluation.metrics import aggregate_metrics
        aggregates = aggregate_metrics(all_metrics)
        for metric in aggregates:
            await self.repository.save_metric(metric)
        baseline_values: dict[str, float] = {}
        if config.baseline_run_id:
            baseline_values = {metric.metric_name: metric.value for metric in await self.repository.list_metrics(config.baseline_run_id) if metric.case_id is None and metric.value is not None}
        gates = evaluate_gates(run_id, config.gates, aggregates, baseline=baseline_values)
        for gate in gates:
            await self.repository.save_gate(gate)
        status = EvaluationStatus.FAILED if failed == len(results) and results else EvaluationStatus.PARTIAL if failed else EvaluationStatus.PASSED
        if any(gate.status == "failed" for gate in gates):
            status = EvaluationStatus.FAILED
        run = replace(run, status=status, completed_count=completed, failed_count=failed, completed_at=_now())
        await self.repository.update_run(run)
        await self._publish("evaluation.completed" if status != EvaluationStatus.FAILED else "evaluation.failed", {"evaluation_run_id": run_id, "status": status, "completed_count": completed, "failed_count": failed})
        return run


__all__ = ["EvaluationService"]
