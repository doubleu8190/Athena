"""Regression and absolute threshold evaluation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from domain.evaluation import GateResult, GateRule, MetricValue


def _compare(actual: float, op: str, expected: float) -> bool:
    return {
        ">=": actual >= expected,
        "<=": actual <= expected,
        "==": actual == expected,
        ">": actual > expected,
        "<": actual < expected,
    }[op]


def evaluate_gates(
    run_id: str,
    rules: Iterable[GateRule],
    metrics: Iterable[MetricValue],
    *,
    baseline: Mapping[str, float] | None = None,
) -> tuple[GateResult, ...]:
    values = {metric.metric_name: metric.value for metric in metrics if metric.case_id is None}
    baseline_values = baseline or {}
    results: list[GateResult] = []
    for rule in rules:
        actual = values.get(rule.metric)
        expected = rule.value
        baseline_value = baseline_values.get(rule.metric)
        if rule.metric.startswith("regression."):
            source_metric = rule.metric.removeprefix("regression.").removesuffix("_delta")
            source_actual = values.get(source_metric)
            baseline_value = baseline_values.get(source_metric)
            actual = None if source_actual is None or baseline_value is None else source_actual - baseline_value
        if actual is None:
            results.append(GateResult(run_id, rule.metric, "failed" if rule.required else "warning", None, rule, baseline_value, "metric is missing"))
            continue
        passed = _compare(float(actual), rule.op, expected)
        results.append(GateResult(run_id, rule.metric, "passed" if passed else ("failed" if rule.required else "warning"), float(actual), rule, baseline_value, None if passed else f"{actual} {rule.op} {expected} is false"))
    return tuple(results)


__all__ = ["evaluate_gates"]
