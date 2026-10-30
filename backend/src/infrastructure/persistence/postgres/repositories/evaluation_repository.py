"""PostgreSQL repository for evaluation datasets, runs and evidence."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.evaluation import (
    AgentExpectation,
    AnswerExpectation,
    CaseAttempt,
    CaseResult,
    DatasetManifest,
    EvalCase,
    EvalRun,
    ExecutionArtifact,
    GateResult,
    GateRule,
    MetricValue,
    RelevanceLabel,
    RetrievalExpectation,
)
from shared import json_value, new_id

from ..models import (
    EvalCaseAttemptModel,
    EvalCaseModel,
    EvalDatasetModel,
    EvalDatasetVersionModel,
    EvalGateModel,
    EvalMetricValueModel,
    EvalRunModel,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _case_payload(case: EvalCase) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "task_type": case.task_type,
        "input": dict(case.input),
        "retrieval": None if case.retrieval is None else {"scope": case.retrieval.scope, "relevant_sources": [{"source_id": item.source_id, "grade": item.grade} for item in case.retrieval.relevant_sources]},
        "answer": None if case.answer is None else {"reference": case.answer.reference, "must_include": list(case.answer.must_include), "must_not_include": list(case.answer.must_not_include), "json_schema": case.answer.json_schema},
        "agent": None if case.agent is None else {"allowed_tools": list(case.agent.allowed_tools), "required_tools": list(case.agent.required_tools), "forbidden_tools": list(case.agent.forbidden_tools), "expected_effects": dict(case.agent.expected_effects)},
        "tags": list(case.tags), "metadata": dict(case.metadata),
    }


def _case_from_json(value: dict[str, Any]) -> EvalCase:
    retrieval_value = value.get("retrieval")
    retrieval = None if retrieval_value is None else RetrievalExpectation(str(retrieval_value.get("scope", "knowledge")), tuple(RelevanceLabel(str(item["source_id"]), int(item.get("grade", 2))) for item in retrieval_value.get("relevant_sources", ())))
    answer_value = value.get("answer")
    answer = None if answer_value is None else AnswerExpectation(answer_value.get("reference"), tuple(answer_value.get("must_include", ())), tuple(answer_value.get("must_not_include", ())), answer_value.get("json_schema"))
    agent_value = value.get("agent")
    agent = None if agent_value is None else AgentExpectation(tuple(agent_value.get("allowed_tools", ())), tuple(agent_value.get("required_tools", ())), tuple(agent_value.get("forbidden_tools", ())), dict(agent_value.get("expected_effects") or {}))
    return EvalCase(str(value["case_id"]), str(value.get("task_type", "retrieval")), dict(value.get("input") or {}), retrieval, answer, agent, tuple(value.get("tags", ())), dict(value.get("metadata") or {}))


class PostgresEvaluationRepository:
    def __init__(self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> None:
        self._session_factory = session_factory

    async def create_manifest(self, manifest: DatasetManifest) -> None:
        now = _now()
        async with self._session_factory() as session:
            async with session.begin():
                if await session.get(EvalDatasetVersionModel, manifest.version_id) is not None:
                    return
                dataset_id = manifest.dataset_name
                if await session.get(EvalDatasetModel, dataset_id) is None:
                    session.add(EvalDatasetModel(dataset_id=dataset_id, name=manifest.dataset_name, created_at=now, updated_at=now))
                session.add(EvalDatasetVersionModel(version_id=manifest.version_id, dataset_id=dataset_id, version=manifest.version_id, manifest_sha256=manifest.manifest_sha256, status="frozen", case_count=manifest.case_count, schema_version=manifest.schema_version, manifest_json=json_value({"corpus": dict(manifest.corpus), "tag_counts": dict(manifest.tag_counts)}), created_at=now))
                for case in manifest.cases:
                    session.add(EvalCaseModel(case_id=case.case_id, version_id=manifest.version_id, task_type=case.task_type, case_json=json_value(_case_payload(case)), tags_json=list(case.tags), created_at=now))

    async def get_manifest(self, version_id: str) -> DatasetManifest | None:
        async with self._session_factory() as session:
            version = await session.get(EvalDatasetVersionModel, version_id)
            if version is None:
                return None
            rows = (await session.execute(select(EvalCaseModel).where(EvalCaseModel.version_id == version_id).order_by(EvalCaseModel.case_id))).scalars().all()
            info = version.manifest_json
            cases = tuple(_case_from_json(row.case_json) for row in rows)
            return DatasetManifest(version_id=version.version_id, dataset_name=version.dataset_id, manifest_sha256=version.manifest_sha256, schema_version=version.schema_version, case_count=version.case_count, tag_counts=dict(info.get("tag_counts") or {}), cases=cases, corpus=dict(info.get("corpus") or {}))

    async def list_manifests(self) -> Sequence[DatasetManifest]:
        async with self._session_factory() as session:
            rows = (await session.execute(select(EvalDatasetVersionModel).order_by(EvalDatasetVersionModel.created_at.desc()))).scalars().all()
            values = []
            for row in rows:
                manifest = await self.get_manifest(row.version_id)
                if manifest is not None:
                    values.append(manifest)
            return tuple(values)

    async def create_run(self, run: EvalRun) -> None:
        now = _now()
        async with self._session_factory() as session:
            async with session.begin():
                session.add(EvalRunModel(run_id=run.run_id, version_id=run.dataset_version, mode=str(run.mode), status=str(run.status), name=run.name, git_sha=run.git_sha, config_json=json_value(dict(run.config)), baseline_run_id=run.baseline_run_id, case_count=run.case_count, completed_count=run.completed_count, failed_count=run.failed_count, started_at=run.started_at, completed_at=run.completed_at, created_at=now, error_message=run.error_message))

    async def update_run(self, run: EvalRun) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(EvalRunModel, run.run_id)
                if row is None:
                    raise KeyError(run.run_id)
                row.status = str(run.status); row.completed_count = run.completed_count; row.failed_count = run.failed_count; row.completed_at = run.completed_at; row.error_message = run.error_message

    async def cancel_run(self, run_id: str) -> EvalRun | None:
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(EvalRunModel, run_id)
                if row is None:
                    return None
                if row.status in {"passed", "failed", "partial", "cancelled"}:
                    return self._run(row)
                row.status = "cancelled"; row.completed_at = _now()
                return self._run(row)

    async def save_case_result(self, result: CaseResult) -> None:
        artifact = result.attempt.artifact
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(EvalCaseAttemptModel, result.attempt.attempt_id)
                if row is None:
                    run = await session.get(EvalRunModel, result.run_id)
                    if run is None:
                        raise KeyError(result.run_id)
                    trace = {**dict(artifact.trace), "ranking": list(artifact.ranking)}
                    session.add(EvalCaseAttemptModel(attempt_id=result.attempt.attempt_id, run_id=result.run_id, version_id=run.version_id, case_id=result.case_id, attempt_number=result.attempt.attempt_number, status=result.status, input_snapshot_json=json_value(dict(result.attempt.input_snapshot)), output_text=artifact.output_text, trace_json=json_value(trace), retrieval_run_id=artifact.retrieval_run_id, agent_run_id=artifact.agent_run_id, duration_ms=artifact.duration_ms, input_tokens=artifact.input_tokens, output_tokens=artifact.output_tokens, estimated_cost=artifact.estimated_cost, error_message=result.error_message, created_at=result.attempt.created_at or _now()))

    async def save_metric(self, metric: MetricValue) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                run = await session.get(EvalRunModel, metric.run_id)
                if run is None:
                    raise KeyError(metric.run_id)
                session.add(EvalMetricValueModel(metric_id=new_id("metric"), run_id=metric.run_id, version_id=run.version_id, case_id=metric.case_id, metric_name=metric.metric_name, value_numeric=metric.value, value_json=json_value(dict(metric.value_json)), evaluator_version=metric.evaluator_version, evidence_json=list(metric.evidence), created_at=_now()))

    async def save_gate(self, gate: GateResult) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                session.add(EvalGateModel(gate_id=new_id("gate"), run_id=gate.run_id, metric=gate.metric, status=gate.status, rule_json={"metric": gate.rule.metric, "op": gate.rule.op, "value": gate.rule.value, "required": gate.rule.required}, actual=gate.actual, baseline=gate.baseline, reason=gate.reason, created_at=_now()))

    async def get_run(self, run_id: str) -> EvalRun | None:
        async with self._session_factory() as session:
            row = await session.get(EvalRunModel, run_id)
            return None if row is None else self._run(row)

    async def list_runs(self, *, limit: int = 50, offset: int = 0, status: str | None = None, dataset_version: str | None = None) -> tuple[Sequence[EvalRun], int]:
        async with self._session_factory() as session:
            query = select(EvalRunModel)
            count_query = select(func.count()).select_from(EvalRunModel)
            if status:
                query = query.where(EvalRunModel.status == status); count_query = count_query.where(EvalRunModel.status == status)
            if dataset_version:
                query = query.where(EvalRunModel.version_id == dataset_version); count_query = count_query.where(EvalRunModel.version_id == dataset_version)
            total = int((await session.execute(count_query)).scalar_one())
            rows = (await session.execute(query.order_by(EvalRunModel.created_at.desc()).offset(offset).limit(limit))).scalars().all()
            return tuple(self._run(row) for row in rows), total

    async def list_case_results(self, run_id: str) -> Sequence[CaseResult]:
        async with self._session_factory() as session:
            rows = (await session.execute(select(EvalCaseAttemptModel).where(EvalCaseAttemptModel.run_id == run_id).order_by(EvalCaseAttemptModel.case_id, EvalCaseAttemptModel.attempt_number))).scalars().all()
            return tuple(CaseResult(row.run_id, row.case_id, row.status, CaseAttempt(row.attempt_id, row.run_id, row.case_id, row.attempt_number, row.status, ExecutionArtifact(row.output_text, tuple(row.trace_json.get("ranking", ())), row.retrieval_run_id, row.agent_run_id, row.trace_json, row.duration_ms, row.input_tokens, row.output_tokens, row.estimated_cost, row.error_message), row.created_at, row.input_snapshot_json), (), row.error_message) for row in rows)

    async def list_metrics(self, run_id: str, *, metric_name: str | None = None) -> Sequence[MetricValue]:
        async with self._session_factory() as session:
            query = select(EvalMetricValueModel).where(EvalMetricValueModel.run_id == run_id)
            if metric_name:
                query = query.where(EvalMetricValueModel.metric_name == metric_name)
            rows = (await session.execute(query.order_by(EvalMetricValueModel.metric_name, EvalMetricValueModel.case_id))).scalars().all()
            return tuple(MetricValue(row.run_id, row.metric_name, row.value_numeric, row.case_id, row.value_json, row.evaluator_version, tuple(row.evidence_json)) for row in rows)

    async def list_gates(self, run_id: str) -> Sequence[GateResult]:
        async with self._session_factory() as session:
            rows = (await session.execute(select(EvalGateModel).where(EvalGateModel.run_id == run_id).order_by(EvalGateModel.metric))).scalars().all()
            return tuple(self._gate(row) for row in rows)

    @staticmethod
    def _run(row: EvalRunModel) -> EvalRun:
        return EvalRun(row.run_id, row.version_id, row.mode, row.status, row.name, row.config_json, row.git_sha, row.baseline_run_id, row.case_count, row.completed_count, row.failed_count, row.started_at, row.completed_at, row.error_message)

    @staticmethod
    def _gate(row: EvalGateModel) -> GateResult:
        rule = row.rule_json
        return GateResult(row.run_id, row.metric, row.status, row.actual, GateRule(str(rule.get("metric", row.metric)), str(rule.get("op", ">=")), float(rule.get("value", 0)), bool(rule.get("required", True))), row.baseline, row.reason)


__all__ = ["PostgresEvaluationRepository"]
