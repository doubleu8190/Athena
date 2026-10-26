#!/usr/bin/env python3
"""Run and validate Athena evaluation datasets."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from application.evaluation import EvaluationService
from application.evaluation.reporting import write_report
from domain.evaluation import EvaluationConfig, EvaluationMode, GateRule
from domain.evaluation.errors import DatasetValidationError
from infrastructure.evaluation import load_dataset
from infrastructure.evaluation.memory_repository import InMemoryEvaluationRepository
from infrastructure.evaluation.retrieval_target import EmbeddingRetrievalTarget, StaticRankingTarget


def _config(path: Path, dataset_version: str | None = None) -> EvaluationConfig:
    try:
        import yaml
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ValueError(f"cannot read config: {path}") from exc
    gates = tuple(GateRule(str(item["metric"]), str(item["op"]), float(item["value"]), bool(item.get("required", True))) for item in value.get("gates", ()))
    return EvaluationConfig(
        name=str(value.get("name", path.stem)),
        dataset_version=dataset_version or str(value.get("dataset", "")),
        mode=EvaluationMode(str(value.get("mode", "retrieval"))),
        target=dict(value.get("target") or {}),
        evaluators=tuple(str(item) for item in value.get("evaluators", ("retrieval_metrics",))),
        gates=gates,
        concurrency=int(value.get("concurrency", 1)),
        timeout_seconds=float(value.get("timeout_seconds", 60)),
        max_attempts=int(value.get("max_attempts", 1)),
        baseline_run_id=value.get("baseline_run_id"),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.evaluate")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-dataset")
    validate.add_argument("path", type=Path)
    validate.add_argument("--name")
    run = commands.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--dataset", type=Path)
    run.add_argument("--output", type=Path, default=Path("data/evaluations/runs"))
    report = commands.add_parser("report")
    report.add_argument("--run", type=Path, required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("--run", type=Path, required=True)
    compare.add_argument("--baseline", type=Path, required=True)
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command == "validate-dataset":
        manifest = load_dataset(args.path, dataset_name=args.name)
        print(json.dumps({"dataset_name": manifest.dataset_name, "version_id": manifest.version_id, "manifest_sha256": manifest.manifest_sha256, "case_count": manifest.case_count, "tag_counts": manifest.tag_counts}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "run":
        dataset_path = args.dataset or Path("tests/retrieval_eval/chinese_cases.jsonl")
        manifest = load_dataset(dataset_path)
        if any(case.task_type == "memory_acceptance" for case in manifest.cases):
            raise ValueError(
                "memory_acceptance datasets require a memory replay target and "
                "acceptance evaluator; the retrieval CLI cannot score them"
            )
        config = _config(args.config, manifest.version_id)
        if config.mode != EvaluationMode.RETRIEVAL:
            raise ValueError("CLI MVP currently supports mode=retrieval")
        repository = InMemoryEvaluationRepository()
        await repository.create_manifest(manifest)
        rankings = (config.target.get("rankings") or {}) if isinstance(config.target, dict) else {}
        if not rankings:
            # The sample CLI target is deterministic; production bootstrap
            # replaces it with the configured embedding target.
            rankings = {case.case_id: tuple(manifest.corpus) for case in manifest.cases}
        target = StaticRankingTarget(rankings)
        service = EvaluationService(repository, target=target)
        run = await service.run(manifest, config)
        results = list(await repository.list_case_results(run.run_id))
        metrics = list(await repository.list_metrics(run.run_id))
        gates = list(await repository.list_gates(run.run_id))
        output = write_report(args.output, run, results, metrics, gates)
        print(json.dumps({"run_id": run.run_id, "status": str(run.status), "output": str(output)}, ensure_ascii=False))
        if any(gate.status == "failed" for gate in gates):
            return 1
        if run.failed_count:
            return 3
        return 0
    if args.command == "report":
        path = args.run / "report.json" if args.run.is_dir() else args.run
        print(path.read_text(encoding="utf-8"))
        return 0
    if args.command == "compare":
        current = json.loads((args.run / "report.json" if args.run.is_dir() else args.run).read_text(encoding="utf-8"))
        baseline = json.loads((args.baseline / "report.json" if args.baseline.is_dir() else args.baseline).read_text(encoding="utf-8"))
        current_values = {item["metric_name"]: item["value"] for item in current.get("metrics", []) if item.get("case_id") is None}
        baseline_values = {item["metric_name"]: item["value"] for item in baseline.get("metrics", []) if item.get("case_id") is None}
        names = sorted(set(current_values) | set(baseline_values))
        print(json.dumps({"items": [{"metric_name": name, "value": current_values.get(name), "baseline": baseline_values.get(name), "delta": None if current_values.get(name) is None or baseline_values.get(name) is None else current_values[name] - baseline_values[name]} for name in names]}, ensure_ascii=False, indent=2))
        return 0
    return 2


def main() -> int:
    try:
        return asyncio.run(_run(_parser().parse_args()))
    except (DatasetValidationError, ValueError) as exc:
        print(f"evaluation error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
