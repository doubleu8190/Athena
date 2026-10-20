from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from application.evaluation import EvaluationService
from application.evaluation.gates import evaluate_gates
from domain.evaluation import EvalCase, EvaluationConfig, EvaluationMode, ExecutionArtifact, GateRule, MetricValue
from domain.evaluation.errors import DatasetValidationError
from infrastructure.evaluation import load_dataset
from infrastructure.evaluation.memory_repository import InMemoryEvaluationRepository
from infrastructure.evaluation.retrieval_target import StaticRankingTarget


DATASET = Path(__file__).parents[1] / "retrieval_eval" / "chinese_cases.jsonl"


def test_dataset_manifest_is_stable_and_supports_legacy_fixture() -> None:
    first = load_dataset(DATASET)
    second = load_dataset(DATASET)
    assert first.version_id == second.version_id
    assert first.case_count == 8
    assert len(first.corpus) == 6
    assert first.cases[0].case_id == "db-cn-001"


def test_retrieval_metrics_deduplicate_ranking() -> None:
    case = EvalCase("c", "retrieval", {"query": "q"}, retrieval=load_dataset(DATASET).cases[0].retrieval)
    from domain.evaluation.metrics import evaluate_retrieval_case
    values = evaluate_retrieval_case(case, ExecutionArtifact(ranking=("x", "db", "db")), run_id="r")
    assert next(item.value for item in values if item.metric_name == "retrieval.mrr") == 0.5


def test_gate_supports_absolute_and_regression_delta() -> None:
    rules = (GateRule("quality", ">=", 0.8), GateRule("regression.quality_delta", ">=", -0.02))
    metrics = (MetricValue("run", "quality", 0.85),)
    result = evaluate_gates("run", rules, metrics, baseline={"quality": 0.86})
    assert [item.status for item in result] == ["passed", "passed"]


@pytest.mark.asyncio
async def test_service_records_partial_case_failures_and_aggregates() -> None:
    manifest = load_dataset(DATASET)

    class Target:
        async def execute(self, case, config):
            if case is manifest.cases[0]:
                raise RuntimeError("fixture failure")
            return ExecutionArtifact(ranking=tuple(manifest.corpus), duration_ms=1)

    repository = InMemoryEvaluationRepository()
    run = await EvaluationService(repository, target=Target()).run(
        manifest,
        EvaluationConfig("test", manifest.version_id, EvaluationMode.RETRIEVAL, {}, ("retrieval_metrics",), max_attempts=1),
    )
    assert str(run.status) == "partial"
    assert run.failed_count == 1
    assert any(item.case_id is None and item.metric_name == "retrieval.recall_at_5" for item in await repository.list_metrics(run.run_id))
