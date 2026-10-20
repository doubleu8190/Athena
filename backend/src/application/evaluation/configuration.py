"""Translate transport-neutral evaluation request values into domain config."""

from __future__ import annotations

from typing import Any

from domain.evaluation import EvaluationConfig, EvaluationMode, GateRule


def make_gate_rules(values: list[dict[str, Any]]) -> tuple[GateRule, ...]:
    return tuple(GateRule(str(item["metric"]), str(item["op"]), float(item["value"]), bool(item.get("required", True))) for item in values)


def make_evaluation_config(value: dict[str, Any], dataset_version: str) -> EvaluationConfig:
    gates = make_gate_rules(value.get("gates", ()))
    return EvaluationConfig(
        name=str(value.get("name", "evaluation")),
        dataset_version=dataset_version,
        mode=EvaluationMode(str(value.get("mode", "retrieval"))),
        target=dict(value.get("target") or {}),
        evaluators=tuple(str(item) for item in value.get("evaluators", ("retrieval_metrics",))),
        gates=gates,
        concurrency=int(value.get("concurrency", 1)),
        timeout_seconds=float(value.get("timeout_seconds", 60)),
        max_attempts=int(value.get("max_attempts", 1)),
        baseline_run_id=value.get("baseline_run_id"),
    )


__all__ = ["make_evaluation_config", "make_gate_rules"]
