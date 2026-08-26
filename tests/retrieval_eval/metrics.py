"""Offline retrieval evaluation data contracts and metric calculations."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


@dataclass(frozen=True)
class RelevantItem:
    """One graded relevant result, optionally with an expected locator."""

    item_id: str
    gain: int = 1
    locator: dict[str, Any] | None = None


@dataclass(frozen=True)
class RetrievalEvalCase:
    """Labeled query used by the offline evaluator."""

    case_id: str
    query: str
    source: str
    attachment_ids: tuple[str, ...] = ()
    query_labels: tuple[str, ...] = ()
    relevant: tuple[RelevantItem, ...] = ()
    must_not_return: tuple[str, ...] = ()
    expect_empty: bool = False
    notes: str = ""


@dataclass(frozen=True)
class RankedResult:
    """A ranked result supplied to evaluation without source content."""

    item_id: str
    locator: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvaluationOutcome:
    """Candidate and final results for one evaluated query."""

    case_id: str
    candidates: tuple[RankedResult, ...]
    final_results: tuple[RankedResult, ...]
    stage_durations_ms: dict[str, float] = field(default_factory=dict)
    llm_calls: int = 0
    llm_tokens: int = 0
    total_duration_ms: float = 0.0


def recall_at_k(results: Sequence[str], relevant: set[str], k: int) -> float:
    """Return the fraction of relevant IDs found in the first ``k`` results."""
    if not relevant:
        return 1.0
    return len(set(results[:k]) & relevant) / len(relevant)


def hit_at_k(results: Sequence[str], relevant: set[str], k: int) -> float:
    """Return 1 when any relevant item is present in the first ``k`` results."""
    return float(bool(set(results[:k]) & relevant))


def reciprocal_rank(results: Sequence[str], relevant: set[str]) -> float:
    """Return the reciprocal rank of the first relevant result."""
    for rank, item_id in enumerate(results, 1):
        if item_id in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(results: Sequence[str], gains: dict[str, int], k: int) -> float:
    """Calculate normalized discounted cumulative gain for the first ``k``."""
    if not gains:
        return 1.0
    dcg = sum(
        (2 ** gains.get(item_id, 0) - 1) / math.log2(rank + 1)
        for rank, item_id in enumerate(results[:k], 1)
    )
    ideal = sorted(gains.values(), reverse=True)[:k]
    idcg = sum(
        (2 ** gain - 1) / math.log2(rank + 1)
        for rank, gain in enumerate(ideal, 1)
    )
    return dcg / idcg if idcg else 0.0


def precision_at_k(results: Sequence[str], relevant: set[str], k: int) -> float:
    """Calculate precision using ``k`` as the fixed denominator."""
    if k <= 0:
        return 0.0
    return len(set(results[:k]) & relevant) / k


def percentile(values: Iterable[float], quantile: float) -> float:
    """Linearly interpolated percentile for a finite sequence."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def build_report(
    cases: Sequence[RetrievalEvalCase], outcomes: Sequence[EvaluationOutcome]
) -> dict[str, Any]:
    """Build aggregate and per-label retrieval metrics."""
    outcomes_by_id = {outcome.case_id: outcome for outcome in outcomes}
    missing = [case.case_id for case in cases if case.case_id not in outcomes_by_id]
    if missing:
        raise ValueError(f"Missing evaluation outcomes: {', '.join(missing)}")
    return {
        "case_count": len(cases),
        "overall": _summarize(cases, outcomes_by_id),
        "by_label": {
            label: _summarize(
                [case for case in cases if label in case.query_labels], outcomes_by_id
            )
            for label in sorted({label for case in cases for label in case.query_labels})
        },
    }


def _summarize(
    cases: Sequence[RetrievalEvalCase],
    outcomes_by_id: dict[str, EvaluationOutcome],
) -> dict[str, Any]:
    if not cases:
        return {"case_count": 0}

    candidate_recalls: dict[int, list[float]] = {10: [], 30: [], 50: []}
    final_hits: dict[int, list[float]] = {1: [], 3: []}
    final_mrr: list[float] = []
    final_ndcg: list[float] = []
    final_precision: list[float] = []
    harmful_hits: list[float] = []
    expected_empty = 0
    empty_false_positive = 0
    non_empty = 0
    empty_false_negative = 0
    locator_total = 0
    locator_correct = 0
    durations: list[float] = []
    stage_durations: dict[str, list[float]] = {}
    llm_calls = 0
    llm_tokens = 0

    for case in cases:
        outcome = outcomes_by_id[case.case_id]
        candidate_ids = [result.item_id for result in outcome.candidates]
        final_ids = [result.item_id for result in outcome.final_results]
        relevant_ids = {item.item_id for item in case.relevant}
        gains = {item.item_id: item.gain for item in case.relevant}

        if relevant_ids:
            for k in candidate_recalls:
                candidate_recalls[k].append(recall_at_k(candidate_ids, relevant_ids, k))
            for k in final_hits:
                final_hits[k].append(hit_at_k(final_ids, relevant_ids, k))
            final_mrr.append(reciprocal_rank(final_ids, relevant_ids))
            final_ndcg.append(ndcg_at_k(final_ids, gains, 10))
            final_precision.append(precision_at_k(final_ids, relevant_ids, 5))

        harmful_hits.append(float(bool(set(final_ids) & set(case.must_not_return))))
        if case.expect_empty:
            expected_empty += 1
            empty_false_positive += int(bool(final_ids))
        elif relevant_ids:
            non_empty += 1
            empty_false_negative += int(not final_ids)

        by_id = {result.item_id: result for result in outcome.final_results}
        for item in case.relevant:
            if item.locator is None:
                continue
            locator_total += 1
            actual = by_id.get(item.item_id)
            if actual is not None and all(
                actual.locator.get(key) == value for key, value in item.locator.items()
            ):
                locator_correct += 1

        durations.append(outcome.total_duration_ms)
        for stage, duration in outcome.stage_durations_ms.items():
            stage_durations.setdefault(stage, []).append(duration)
        llm_calls += outcome.llm_calls
        llm_tokens += outcome.llm_tokens

    return {
        "case_count": len(cases),
        "candidate": {
            f"recall_at_{k}": _mean(values) for k, values in candidate_recalls.items()
        },
        "final": {
            "hit_at_1": _mean(final_hits[1]),
            "hit_at_3": _mean(final_hits[3]),
            "mrr": _mean(final_mrr),
            "ndcg_at_10": _mean(final_ndcg),
            "precision_at_5": _mean(final_precision),
        },
        "negative": {
            "harmful_hit_rate": _mean(harmful_hits),
            "empty_false_positive_rate": (
                empty_false_positive / expected_empty if expected_empty else 0.0
            ),
            "empty_false_negative_rate": (
                empty_false_negative / non_empty if non_empty else 0.0
            ),
        },
        "locator_accuracy": locator_correct / locator_total if locator_total else 0.0,
        "latency_ms": {
            "p50": percentile(durations, 0.50),
            "p95": percentile(durations, 0.95),
        },
        "stage_latency_ms": {
            stage: {"p50": percentile(values, 0.50), "p95": percentile(values, 0.95)}
            for stage, values in sorted(stage_durations.items())
        },
        "llm": {"calls": llm_calls, "estimated_tokens": llm_tokens},
    }


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
