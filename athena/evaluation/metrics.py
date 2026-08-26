"""正式评估用例的可审计检索质量指标。"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

from athena.evaluation.models import EvalCase, RetrievalOutcome
from athena.evaluation.relevance import matched_items, prohibited_hit


def percentile(values: Iterable[float], quantile: float) -> float:
    """计算线性插值百分位数。

    参数：
        values: 数值迭代器。
        quantile: 0 到 1 之间的分位位置。

    返回值：
        百分位值；输入为空时返回 0.0。

    异常：
        ValueError: ``quantile`` 超出闭区间 [0, 1]。
    """
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1")
    position = (len(ordered) - 1) * quantile
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _mean(values: Sequence[float]) -> float:
    """计算序列均值，空序列返回 0.0。"""
    return sum(values) / len(values) if values else 0.0


def _result_slice(
    outcome: RetrievalOutcome, k: int, candidate: bool
) -> tuple[Any, ...]:
    """返回结果中的前 ``k`` 项候选集或最终集。"""
    return (outcome.candidates if candidate else outcome.final_results)[:k]


def case_metrics(case: EvalCase, outcome: RetrievalOutcome) -> dict[str, float | None]:
    """计算单个用例的安全指标，供 paired comparison 使用。"""
    relevant_count = len(case.relevant)
    candidate_metrics: dict[str, float] = {}
    for k in (10, 30, 50):
        candidate_metrics[f"candidate.recall_at_{k}"] = (
            len(matched_items(case, outcome.candidates[:k])) / relevant_count
            if relevant_count
            else 0.0
        )
    final_matches = matched_items(case, outcome.final_results)
    ranks = [
        rank
        for rank, result in enumerate(outcome.final_results, 1)
        if matched_items(case, (result,))
    ]
    gains = [
        next(
            (item.gain for index, item in enumerate(case.relevant) if index in matched_items(case, (result,))),
            0,
        )
        for result in outcome.final_results[:10]
    ]
    dcg = sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))
    ideal = sorted((item.gain for item in case.relevant), reverse=True)[:10]
    idcg = sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(ideal, 1))
    locator_items = [item for item in case.relevant if item.locator is not None]
    locator_accuracy = (
        sum(
            any(item.locator.matches(result.locator) for result in outcome.final_results)
            for item in locator_items
        )
        / len(locator_items)
        if locator_items
        else None
    )
    return {
        **candidate_metrics,
        "final.hit_at_1": float(bool(matched_items(case, outcome.final_results[:1]))) if relevant_count else 0.0,
        "final.hit_at_3": float(bool(matched_items(case, outcome.final_results[:3]))) if relevant_count else 0.0,
        "final.mrr": 1.0 / min(ranks) if ranks else 0.0,
        "final.ndcg_at_10": dcg / idcg if idcg else 0.0,
        "final.precision_at_5": len(matched_items(case, outcome.final_results[:5])) / 5 if relevant_count else 0.0,
        "locator_accuracy": locator_accuracy,
        "negative.harmful_hit_rate": float(prohibited_hit(case, outcome.final_results)),
        "negative.empty_false_positive_rate": float(bool(outcome.final_results)) if case.expect_empty else None,
        "negative.empty_false_negative_rate": float(not outcome.final_results) if relevant_count and not case.expect_empty else None,
        "latency.p95": outcome.total_duration_ms,
    }


def summarize(
    cases: Sequence[EvalCase], outcomes: Sequence[RetrievalOutcome]
) -> dict[str, Any]:
    """汇总召回、排序、负例和延迟指标。

    参数：
        cases: 评估用例序列。
        outcomes: 与用例通过 ``case_id`` 对应的检索结果。

    返回值：
        可直接写入报告的整体及分组指标字典。

    异常：
        KeyError: 结果缺少对应的用例。
    """
    by_id = {outcome.case_id: outcome for outcome in outcomes}
    missing = [case.case_id for case in cases if case.case_id not in by_id]
    if missing:
        raise ValueError(f"Missing outcomes: {', '.join(missing)}")
    recall: dict[int, list[float]] = {10: [], 30: [], 50: []}
    hits: dict[int, list[float]] = {1: [], 3: []}
    mrr: list[float] = []
    ndcg: list[float] = []
    precision: list[float] = []
    harmful: list[float] = []
    empty_cases = false_positive = non_empty = false_negative = 0
    durations = [by_id[case.case_id].total_duration_ms for case in cases]
    stage_values: dict[str, list[float]] = {}
    locator_total = locator_correct = 0
    for case in cases:
        outcome = by_id[case.case_id]
        relevant_count = len(case.relevant)
        if relevant_count:
            for k in recall:
                recall[k].append(
                    len(matched_items(case, _result_slice(outcome, k, True)))
                    / relevant_count
                )
            for k in hits:
                hits[k].append(
                    float(bool(matched_items(case, _result_slice(outcome, k, False))))
                )
            ranks = [
                rank
                for rank, result in enumerate(outcome.final_results, 1)
                if matched_items(case, (result,))
            ]
            mrr.append(1.0 / min(ranks) if ranks else 0.0)
            gains = [item.gain for item in case.relevant]
            actual_gains = [
                next(
                    (
                        item.gain
                        for index, item in enumerate(case.relevant)
                        if index in matched_items(case, (result,))
                    ),
                    0,
                )
                for result in outcome.final_results[:10]
            ]
            dcg = sum(
                (2**gain - 1) / math.log2(rank + 1)
                for rank, gain in enumerate(actual_gains, 1)
            )
            ideal = sorted(gains, reverse=True)[:10]
            idcg = sum(
                (2**gain - 1) / math.log2(rank + 1)
                for rank, gain in enumerate(ideal, 1)
            )
            ndcg.append(dcg / idcg if idcg else 0.0)
            precision.append(len(matched_items(case, outcome.final_results[:5])) / 5)
            non_empty += 1
            false_negative += int(not outcome.final_results)
        if case.expect_empty:
            empty_cases += 1
            false_positive += int(bool(outcome.final_results))
        harmful.append(float(prohibited_hit(case, outcome.final_results)))
        for item in case.relevant:
            if item.locator is None:
                continue
            locator_total += 1
            locator_correct += int(
                any(
                    item.locator.matches(result.locator)
                    for result in outcome.final_results
                )
            )
        for stage, duration in outcome.stage_durations_ms.items():
            stage_values.setdefault(stage, []).append(duration)
    return {
        "case_count": len(cases),
        "candidate": {f"recall_at_{k}": _mean(values) for k, values in recall.items()},
        "final": {
            "hit_at_1": _mean(hits[1]),
            "hit_at_3": _mean(hits[3]),
            "mrr": _mean(mrr),
            "ndcg_at_10": _mean(ndcg),
            "precision_at_5": _mean(precision),
        },
        "negative": {
            "harmful_hit_rate": _mean(harmful),
            "empty_false_positive_rate": (
                false_positive / empty_cases if empty_cases else 0.0
            ),
            "empty_false_negative_rate": (
                false_negative / non_empty if non_empty else 0.0
            ),
        },
        "locator_accuracy": locator_correct / locator_total if locator_total else 0.0,
        "latency_ms": {
            "p50": percentile(durations, 0.5),
            "p95": percentile(durations, 0.95),
        },
        "stage_latency_ms": {
            stage: {"p50": percentile(values, 0.5), "p95": percentile(values, 0.95)}
            for stage, values in sorted(stage_values.items())
        },
    }
