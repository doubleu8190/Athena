from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from athena.evaluation.annotation import AnnotationService, Label
from athena.core.memory.retrieval import HybridRetrievalManager
from athena.evaluation.gate import evaluate_gate
from athena.evaluation.models import Annotation, DatasetManifest, EvalCase, Locator, RetrievalItem
from athena.evaluation.redaction import redact_request
from athena.evaluation.shadow import ShadowRetrievalRunner
from athena.evaluation.storage import JsonlEventStore
from athena.core.files.runtime import FileIntelligenceRuntime
from athena.evaluation.models import RankedResult, RetrievalContext, RetrievalOutcome
from athena.evaluation.report import build_report


def _manifest() -> DatasetManifest:
    return DatasetManifest("dataset", "v1", "2026-08-20T00:00:00Z", "v1", 1, "a" * 64, "1")


def test_core_retrieval_entrypoint_only_exposes_behavior_control_parameter():
    parameters = inspect.signature(HybridRetrievalManager.retrieve).parameters
    assert "record_access" in parameters
    assert not {
        "evaluation_run_id",
        "evaluation_case_id",
        "dataset_version",
        "index_version",
    } & parameters.keys()
    file_parameters = inspect.signature(FileIntelligenceRuntime.search_file).parameters
    assert not {
        "evaluation_run_id",
        "evaluation_case_id",
        "dataset_version",
        "index_version",
    } & file_parameters.keys()


def test_outcome_carries_safe_trace_and_evaluation_context():
    case = EvalCase(
        "trace-case",
        "query",
        {"source": "memory"},
        labels=("memory",),
    )
    outcome = RetrievalOutcome(
        case_id=case.case_id,
        candidates=(RankedResult("memory-1"),),
        final_results=(RankedResult("memory-1"),),
        trace={
            "query_hash": "hash",
            "stage_durations_ms": [],
            "selected_ids": ["memory-1"],
        },
        retrieval_context=RetrievalContext("run-1", case.case_id),
    )
    report = build_report([case], [outcome], pipeline="candidate")
    assert report["outcomes"][0]["retrieval_context"] == {
        "evaluation_run_id": "run-1",
        "evaluation_case_id": "trace-case",
    }
    assert "query_hash" in report["outcomes"][0]["trace"]
    assert "query" not in report["outcomes"][0]["trace"]


def test_locators_validate_supported_source_positions_and_negative_cases_require_review():
    assert Locator.from_dict({"page": 12}).matches({"page": 12})
    assert Locator.from_dict({"sheet": "Summary", "start_row": 2, "end_row": 4})
    assert Locator.from_dict({"path": "src/main.py", "start_line": 10, "end_line": 12})
    with pytest.raises(ValueError):
        EvalCase("empty", "query", {"source": "file"}, expect_empty=True)
    with pytest.raises(ValueError):
        Locator.from_dict({"page": 0})


def test_gate_fails_critical_regression_and_override_is_auditable():
    comparison = {"case_count": 10, "metrics": {"final.hit_at_3": {"baseline": .9, "candidate": .7, "absolute_delta": -.2}}}
    rules = {"metrics": {"final.hit_at_3": {"max_regression": .01}}}
    failed = evaluate_gate(comparison, rules)
    assert failed["status"] == "failed"
    passed = evaluate_gate(comparison, rules, override_reason="approved emergency rollback")
    assert passed["status"] == "passed"
    assert passed["overridden"] is True


def test_redaction_and_shadow_are_non_blocking(tmp_path):
    request = redact_request("find invoice 123", {"source": "memory"}, salt="test")
    assert request.query_hash != "find invoice 123"
    assert "query" not in request.__dict__

    async def executor(_query, _scope, *, record_access):
        assert record_access is False
        return [{"id": "candidate-1"}]

    async def scenario():
        store = JsonlEventStore(tmp_path / "shadow.jsonl")
        runner = ShadowRetrievalRunner(executor, enabled=True, sample_rate=1, store=store)
        assert await runner.submit(query="find invoice 123", scope={"source": "memory"}, baseline_ids=["baseline-1"])
        await runner.close()
        events = list(store.read())
        assert events[0]["shadow_ids"] == ["candidate-1"]
        assert '"query":' not in json.dumps(events)

    asyncio.run(scenario())


def test_annotation_requires_two_labels_and_releases_immutable_case(tmp_path):
    async def unused():
        return None

    store = JsonlEventStore(tmp_path / "annotations.jsonl")
    service = AnnotationService(store)
    for annotator in ("a", "b"):
        service.add_label(Label("task", annotator, 3, True, False, "selection", "2026-08-20T00:00:00Z"))
    arbitration = service.arbitrate("task", arbitrator_id="reviewer")
    case = service.release_case(task_id="task", case_id="case", query="q", scope={"source": "file"}, document_alias="doc", locator={"page": 1}, quote_sha256="b" * 64, arbitrated=arbitration, labels=["pdf"])
    assert case.annotation is not None
    with pytest.raises(ValueError):
        service.add_label(Label("task", "c", 1, False, False, "unknown", "2026-08-20T00:00:00Z"))
