"""Tests for the deterministic offline retrieval evaluator."""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

import pytest

from tests.retrieval_eval.fixtures import FixtureLegacyPipeline
from tests.retrieval_eval.metrics import (
    EvaluationOutcome,
    RankedResult,
    RelevantItem,
    RetrievalEvalCase,
    build_report,
    ndcg_at_k,
    percentile,
)
from tests.retrieval_eval.runner import evaluate_cases, load_cases, render_markdown


def test_case_corpus_has_planned_size_and_label_distribution():
    cases = load_cases(Path("tests/retrieval_eval/cases.jsonl"))

    assert len(cases) == 290
    assert len({case.case_id for case in cases}) == 290
    assert Counter(label for case in cases for label in case.query_labels) == {
        "exact_identifier": 30,
        "exact_value": 20,
        "faq": 30,
        "natural_language": 30,
        "code": 20,
        "error_code": 20,
        "lookup": 20,
        "multi_hop": 20,
        "complex": 20,
        "memory": 20,
        "aggregate": 20,
        "expected_empty": 30,
        "adversarial": 30,
    }
    assert all(
        not case.relevant
        for case in cases
        if "expected_empty" in case.query_labels
    )


def test_metrics_use_graded_relevance_and_linear_percentiles():
    assert ndcg_at_k(["weak", "strong"], {"strong": 3, "weak": 1}, 2) < 1.0
    assert ndcg_at_k(["strong", "weak"], {"strong": 3, "weak": 1}, 2) == 1.0
    assert percentile([1.0, 3.0], 0.95) == pytest.approx(2.9)


def test_report_separates_candidate_final_negative_and_locator_metrics():
    case = RetrievalEvalCase(
        case_id="case-1",
        query="query",
        source="file",
        query_labels=("code",),
        relevant=(RelevantItem("right", gain=3, locator={"path": "a.py"}),),
        must_not_return=("harmful",),
    )
    outcome = EvaluationOutcome(
        case_id="case-1",
        candidates=(RankedResult("right", {"path": "a.py"}),),
        final_results=(RankedResult("right", {"path": "a.py"}),),
        total_duration_ms=12.0,
    )

    report = build_report([case], [outcome])

    assert report["overall"]["candidate"]["recall_at_10"] == 1.0
    assert report["overall"]["final"]["hit_at_1"] == 1.0
    assert report["overall"]["negative"]["harmful_hit_rate"] == 0.0
    assert report["overall"]["locator_accuracy"] == 1.0
    assert report["overall"]["stage_latency_ms"] == {}
    assert "code" in report["by_label"]


def test_fixture_runner_is_deterministic_and_renders_markdown():
    cases_path = Path("tests/retrieval_eval/cases.jsonl")
    cases = load_cases(cases_path)

    first = asyncio.run(evaluate_cases(cases, FixtureLegacyPipeline()))
    second = asyncio.run(evaluate_cases(cases, FixtureLegacyPipeline()))
    report = build_report(cases, first)
    report["pipeline"] = "legacy"

    assert [outcome.final_results for outcome in first] == [
        outcome.final_results for outcome in second
    ]
    assert report["case_count"] == len(cases)
    assert "candidate_retrieval" in report["overall"]["stage_latency_ms"]
    assert "# Retrieval Evaluation Report" in render_markdown(report)
