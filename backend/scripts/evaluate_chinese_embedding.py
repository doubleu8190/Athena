#!/usr/bin/env python3
"""Compatibility entrypoint for the original Chinese embedding benchmark."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from application.evaluation import EvaluationService
from domain.evaluation import EvaluationConfig, EvaluationMode
from infrastructure.evaluation import load_dataset
from infrastructure.evaluation.memory_repository import InMemoryEvaluationRepository
from infrastructure.evaluation.retrieval_target import EmbeddingRetrievalTarget


async def main() -> None:
    fixture_path = Path(__file__).parents[1] / "tests" / "retrieval_eval" / "chinese_cases.jsonl"
    manifest = load_dataset(fixture_path)
    from configuration import get_settings
    from infrastructure.integrations.llm.embedding_provider import ConfiguredEmbeddingProvider

    settings = get_settings()
    repository = InMemoryEvaluationRepository()
    await repository.create_manifest(manifest)
    service = EvaluationService(
        repository,
        target=EmbeddingRetrievalTarget(manifest.corpus, ConfiguredEmbeddingProvider(settings)),
    )
    run = await service.run(
        manifest,
        EvaluationConfig(
            "chinese-embedding",
            manifest.version_id,
            EvaluationMode.RETRIEVAL,
            {"provider": settings.embedding_provider, "model": settings.embedding_model},
            ("retrieval_metrics",),
            concurrency=1,
        ),
    )
    metrics = [item for item in await repository.list_metrics(run.run_id) if item.case_id is None]
    failed_cases = [item for item in await repository.list_case_results(run.run_id) if item.error_message]
    print(json.dumps({"run_id": run.run_id, "status": str(run.status), "embedding": {"provider": settings.embedding_provider, "model": settings.embedding_model}, "metrics": {item.metric_name: item.value for item in metrics}, "failed_cases": [{"case_id": item.case_id, "error": item.error_message} for item in failed_cases]}, ensure_ascii=False, indent=2))
    if str(run.status) != "passed":
        raise RuntimeError(f"evaluation {run.run_id} finished with status {run.status}")


if __name__ == "__main__":
    asyncio.run(main())
