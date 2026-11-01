from __future__ import annotations

from pathlib import Path
from typing import get_type_hints

from fastapi.testclient import TestClient

from bootstrap import create_app
from interfaces.http.controllers.evaluation_controller import build_evaluation_router
from infrastructure.evaluation import load_dataset
from infrastructure.evaluation.memory_repository import InMemoryEvaluationRepository
from infrastructure.evaluation.retrieval_target import StaticRankingTarget


def _client() -> TestClient:
    repository = InMemoryEvaluationRepository()
    return TestClient(create_app(
        evaluation_repository=repository,
        evaluation_dataset_loader=load_dataset,
        evaluation_target_factory=lambda manifest: StaticRankingTarget({case.case_id: tuple(manifest.corpus) for case in manifest.cases}),
    ))


_DATASET_PATH = str(Path(__file__).parents[1] / "retrieval_eval" / "chinese_cases.jsonl")


def test_evaluation_router_annotations_resolve() -> None:
    assert get_type_hints(build_evaluation_router)


def test_evaluation_api_validates_queues_and_reads_run() -> None:
    client = _client()
    validated = client.post("/api/evaluations/datasets/validate", json={"path": _DATASET_PATH})
    assert validated.status_code == 200
    assert validated.json()["case_count"] == 8
    queued = client.post("/api/evaluations/runs", json={"dataset_path": _DATASET_PATH, "name": "api-smoke"})
    assert queued.status_code == 200
    assert queued.json()["status"] == "queued"
    run = client.get(f"/api/evaluations/runs/{queued.json()['run_id']}")
    assert run.status_code == 200
    assert run.json()["case_count"] == 8


def test_evaluation_api_rejects_invalid_gate_and_unknown_run() -> None:
    client = _client()
    assert client.get("/api/evaluations/runs/missing").status_code == 404
    assert client.post("/api/evaluations/runs/missing/gates", json={"gates": [{"metric": "x", "op": "???", "value": 1}]}).status_code == 422
