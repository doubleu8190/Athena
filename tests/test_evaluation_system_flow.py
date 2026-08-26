from __future__ import annotations

import pytest

from athena.config.settings import Settings
from athena.evaluation.bindings import BindingIndex
from athena.evaluation.comparison import compare_reports
from athena.evaluation.models import EvalCase, RankedResult, RetrievalOutcome
from athena.evaluation.privacy import safe_report
from athena.evaluation.report import build_report
from athena.evaluation.snapshot import EvaluationWorkspace


def _report(pipeline: str) -> dict:
    cases = [
        EvalCase(f"case-{index}", f"query-{index}", {"source": "memory"}, relevant=())
        for index in range(2)
    ]
    outcomes = [
        RetrievalOutcome(
            case.case_id,
            candidates=(RankedResult("item"),),
            final_results=(RankedResult("item"),),
            total_duration_ms=float(index + 1),
        )
        for index, case in enumerate(cases)
    ]
    return build_report(
        cases,
        outcomes,
        pipeline=pipeline,
        metadata={
            "dataset_id": "dataset",
            "dataset_version": "v1",
            "snapshot_id": "snapshot",
            "index_version": "index",
            "settings_hash": "settings",
        },
    )


def test_report_contains_paired_case_metrics_without_query_or_content():
    report = _report("candidate")
    assert report["case_ids"] == ["case-0", "case-1"]
    assert report["outcomes"][0]["metrics"]["latency.p95"] == 1.0
    serialized = str(report)
    assert "query-0" not in serialized
    assert "content" not in serialized


def test_comparison_requires_same_case_order_and_has_bootstrap_interval():
    baseline = _report("legacy")
    candidate = _report("candidate")
    comparison = compare_reports(baseline, candidate, iterations=100)
    assert comparison["metrics"]["latency.p95"]["confidence_interval_95"] == (0.0, 0.0)
    candidate["case_ids"] = list(reversed(candidate["case_ids"]))
    with pytest.raises(ValueError, match="case sets differ"):
        compare_reports(baseline, candidate, iterations=100)


def test_workspace_rejects_production_tree_before_creation(tmp_path):
    settings = Settings(
        sqlite_db_path=str(tmp_path / "data" / "athena.db"),
        chromadb_path=str(tmp_path / "data" / "chromadb"),
        file_storage_path=str(tmp_path / "data" / "files"),
    )
    workspace = EvaluationWorkspace(tmp_path / "data" / "evaluation", settings)
    with pytest.raises(RuntimeError, match="overlaps"):
        workspace.create()
    assert not (tmp_path / "data" / "evaluation").exists()


def test_missing_binding_resolves_to_empty_and_nested_sensitive_fields_are_rejected():
    assert BindingIndex().resolve("missing") == []
    assert "api_key" not in str(safe_report({"scope": {"api_key": "secret"}}))
