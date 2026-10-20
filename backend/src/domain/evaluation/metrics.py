"""Deterministic evaluation metrics."""

from __future__ import annotations

import math
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .entities import EvalCase, ExecutionArtifact, MetricValue


def _metric(run_id: str, case_id: str, name: str, value: float | None, **kwargs: Any) -> MetricValue:
    return MetricValue(run_id, name, value, case_id=case_id, **kwargs)


def evaluate_retrieval_case(
    case: EvalCase,
    artifact: ExecutionArtifact,
    *,
    run_id: str,
    ks: Sequence[int] = (1, 3, 5, 10),
) -> tuple[MetricValue, ...]:
    """Return per-case retrieval metrics while preserving ranking order."""
    if case.retrieval is None:
        return ()
    normalized_ks = tuple(dict.fromkeys(int(k) for k in ks))
    if not normalized_ks or any(k <= 0 for k in normalized_ks):
        raise ValueError("ks must contain positive integers")
    labels = {label.source_id: label.grade for label in case.retrieval.relevant_sources if label.grade > 0}
    ranking = tuple(dict.fromkeys(artifact.ranking))
    first = next((index for index, source_id in enumerate(ranking, 1) if source_id in labels), None)
    values: list[MetricValue] = [
        _metric(run_id, case.case_id, "retrieval.mrr", 1.0 / first if first else (None if not labels else 0.0)),
        _metric(run_id, case.case_id, "runtime.duration_ms", artifact.duration_ms),
        _metric(run_id, case.case_id, "runtime.timeout", 1.0 if artifact.error_message == "timeout" else 0.0),
        _metric(run_id, case.case_id, "runtime.error", 1.0 if artifact.error_message else 0.0),
    ]
    total_relevant = len(labels)
    for k in normalized_ks:
        top = ranking[:k]
        found = sum(1 for source_id in labels if source_id in top)
        recall = found / total_relevant if total_relevant else None
        hit = 1.0 if found else 0.0 if total_relevant else None
        gains = [labels.get(source_id, 0) for source_id in top]
        dcg = sum((2**gain - 1) / math.log2(index + 2) for index, gain in enumerate(gains))
        ideal_gains = sorted(labels.values(), reverse=True)[:k]
        idcg = sum((2**gain - 1) / math.log2(index + 2) for index, gain in enumerate(ideal_gains))
        ndcg = dcg / idcg if idcg else None
        values.extend([
            _metric(run_id, case.case_id, f"retrieval.recall_at_{k}", recall),
            _metric(run_id, case.case_id, f"retrieval.hit_rate_at_{k}", hit),
            _metric(run_id, case.case_id, f"retrieval.ndcg_at_{k}", ndcg),
        ])
    return tuple(values)


def evaluate_answer_rules(case: EvalCase, artifact: ExecutionArtifact, *, run_id: str) -> tuple[MetricValue, ...]:
    """Evaluate cheap answer constraints without invoking a judge model."""
    if case.answer is None:
        return ()
    output = artifact.output_text or ""
    required = [item for item in case.answer.must_include if item]
    forbidden = [item for item in case.answer.must_not_include if item]
    include_pass = all(item in output for item in required)
    forbidden_pass = all(item not in output for item in forbidden)
    exact = case.answer.reference is not None and output.strip() == case.answer.reference.strip()
    schema_valid: float | None = None
    if case.answer.json_schema is not None:
        try:
            parsed = json.loads(output)
            schema_valid = 1.0
            required = case.answer.json_schema.get("required", [])
            if isinstance(required, list) and isinstance(parsed, dict):
                schema_valid = 1.0 if all(key in parsed for key in required) else 0.0
        except (TypeError, ValueError, json.JSONDecodeError):
            schema_valid = 0.0
    return (
        _metric(run_id, case.case_id, "answer.must_include_pass", 1.0 if include_pass else 0.0),
        _metric(run_id, case.case_id, "answer.must_not_include_pass", 1.0 if forbidden_pass else 0.0),
        _metric(run_id, case.case_id, "answer.exact_match", 1.0 if exact else 0.0 if case.answer.reference is not None else None),
        _metric(run_id, case.case_id, "answer.json_schema_valid", schema_valid),
    )


def aggregate_metrics(metrics: Iterable[MetricValue]) -> tuple[MetricValue, ...]:
    """Aggregate numeric case metrics by mean, excluding missing values."""
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    run_id = ""
    for metric in metrics:
        run_id = metric.run_id
        if metric.case_id is not None and metric.value is not None:
            grouped[(metric.run_id, metric.metric_name)].append(float(metric.value))
    return tuple(
        MetricValue(
            run_id=run,
            metric_name=name,
            value=sum(values) / len(values) if values else None,
            value_json={"sample_count": len(values)},
            evaluator_version="1",
        )
        for (run, name), values in sorted(grouped.items())
    )


__all__ = ["aggregate_metrics", "evaluate_answer_rules", "evaluate_retrieval_case"]
