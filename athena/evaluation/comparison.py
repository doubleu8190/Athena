"""配对比较和确定性自助法置信区间。"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Any


def paired_bootstrap(
    values: Sequence[float],
    *,
    iterations: int = 10_000,
    seed: int = 0,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """计算配对差值的确定性 bootstrap 置信区间。

    参数：
        values: 每个用例的配对观测值。
        iterations: 重采样次数，必须至少为 100。
        seed: 随机种子，保证报告可复现。
        confidence: 置信水平，取值范围为 0 到 1。

    返回值：
        按低、高分位排列的置信区间元组。

    异常：
        ValueError: 观测值不足、重采样次数过少或置信水平越界。
    """
    if not values:
        raise ValueError("bootstrap requires at least one paired observation")
    if len(values) < 2:
        raise ValueError("bootstrap comparison requires at least two cases")
    if iterations < 100:
        raise ValueError("iterations must be at least 100")
    rng = random.Random(seed)
    samples = [
        sum(rng.choice(values) for _ in values) / len(values) for _ in range(iterations)
    ]
    samples.sort()
    alpha = (1 - confidence) / 2
    low = samples[max(0, int(alpha * iterations))]
    high = samples[min(iterations - 1, int((1 - alpha) * iterations))]
    return low, high


def _relative(delta: float, baseline: float) -> float | None:
    """计算相对变化率；基线为零时返回 ``None``。"""
    return delta / baseline if baseline else None


def compare_reports(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    seed: int = 0,
    iterations: int = 10_000,
) -> dict[str, Any]:
    """比较两份报告并计算逐指标的绝对、相对变化及置信区间。

    参数：
        baseline: 基线流水线报告。
        candidate: 候选流水线报告。
        seed: bootstrap 随机种子。
        iterations: 每个指标的 bootstrap 重采样次数。

    返回值：
        包含报告元数据、整体指标和按标签分组结果的比较字典。

    异常：
        ValueError: 两份报告的数据集、快照、用例集合或索引版本不一致。
        KeyError: 报告缺少必需的指标字段。
    """
    baseline_metadata = {
        **baseline.get("metadata", {}),
        **{
            key: baseline.get(key)
            for key in ("dataset_id", "snapshot_id", "index_version")
            if key in baseline
        },
    }
    candidate_metadata = {
        **candidate.get("metadata", {}),
        **{
            key: candidate.get(key)
            for key in ("dataset_id", "snapshot_id", "index_version")
            if key in candidate
        },
    }
    for key in (
        "dataset_id", "dataset_version", "snapshot_id", "index_version",
        "settings_hash", "model_config", "embedding_model", "llm_model",
        "parser_version", "application_commit",
    ):
        if key in baseline_metadata and key in candidate_metadata and baseline_metadata[key] != candidate_metadata[key]:
            raise ValueError(f"baseline and candidate must share {key}")
    if (
        "index_version" in baseline_metadata
        and "index_version" in candidate_metadata
        and baseline_metadata["index_version"] != candidate_metadata["index_version"]
    ):
        raise ValueError("baseline and candidate must share index_version")
    metric_paths = (
        ("candidate", "recall_at_30"),
        ("final", "hit_at_1"),
        ("final", "hit_at_3"),
        ("final", "mrr"),
        ("final", "ndcg_at_10"),
        ("locator_accuracy",),
        ("negative", "empty_false_positive_rate"),
        ("latency_ms", "p95"),
    )
    baseline_outcomes = {item["case_id"]: item for item in baseline.get("outcomes", [])}
    candidate_outcomes = {
        item["case_id"]: item for item in candidate.get("outcomes", [])
    }
    case_ids = [case_id for case_id in baseline.get("case_ids", baseline_outcomes) if case_id in candidate_outcomes]
    if baseline.get("case_ids") is not None and candidate.get("case_ids") is not None:
        if baseline["case_ids"] != candidate["case_ids"]:
            raise ValueError("baseline and candidate case sets differ")
    if baseline_outcomes and list(baseline_outcomes) != list(candidate_outcomes):
        raise ValueError("baseline and candidate case sets differ")
    result: dict[str, Any] = {
        "schema_version": "1",
        "baseline": baseline.get("pipeline", "baseline"),
        "candidate": candidate.get("pipeline", "candidate"),
        "case_count": len(case_ids) or candidate.get("case_count", 0),
        "metadata": {
            key: candidate.get(key, candidate.get("metadata", {}).get(key))
            for key in (
                "dataset_id",
                "snapshot_id",
                "commit",
                "model_config",
                "index_version",
            )
            if key in candidate or key in candidate.get("metadata", {})
        },
        "case_ids": case_ids,
        "metrics": {},
        "by_label": {},
    }
    for path in metric_paths:
        section = path[0]
        name = path[1] if len(path) > 1 else None
        key = f"{section}.{name}" if name else section
        if section == "latency_ms" and name:
            baseline_value = baseline["overall"].get(
                "latency", baseline["overall"].get("latency_ms", {})
            )[name]
            candidate_value = candidate["overall"].get(
                "latency", candidate["overall"].get("latency_ms", {})
            )[name]
        else:
            baseline_value = (
                baseline["overall"][section][name]
                if name
                else baseline["overall"][section]
            )
            candidate_value = (
                candidate["overall"][section][name]
                if name
                else candidate["overall"][section]
            )
        delta = candidate_value - baseline_value
        # 仅聚合报告仍然有用；可用时使用逐用例自助法。
        if case_ids and name:
            case_metric_key = "latency.p95" if key == "latency_ms.p95" else key
            base_values = []
            candidate_values = []
            for case_id in case_ids:
                baseline_metrics = baseline_outcomes[case_id].get("metrics", {})
                candidate_metrics = candidate_outcomes[case_id].get("metrics", {})
                if case_metric_key not in baseline_metrics or case_metric_key not in candidate_metrics:
                    raise ValueError(f"paired case metrics missing for {key}")
                base_values.append(baseline_metrics[case_metric_key])
                candidate_values.append(candidate_metrics[case_metric_key])
            diffs = [candidate_item - base_item for base_item, candidate_item in zip(base_values, candidate_values) if isinstance(base_item, (int, float)) and isinstance(candidate_item, (int, float))]
            if not diffs:
                interval = (delta, delta)
            else:
                if len(diffs) < 2:
                    raise ValueError(f"bootstrap comparison requires at least two cases for {key}")
                interval = paired_bootstrap(diffs, seed=seed, iterations=iterations)
        else:
            interval = (delta, delta)
        result["metrics"]["latency.p95" if key == "latency_ms.p95" else key] = {
            "baseline": baseline_value,
            "candidate": candidate_value,
            "absolute_delta": delta,
            "relative_delta": _relative(delta, baseline_value),
            "confidence_interval_95": interval,
        }
    for label in sorted(
        set(baseline.get("by_label", {})) & set(candidate.get("by_label", {}))
    ):
        baseline_summary = baseline["by_label"][label]
        candidate_summary = candidate["by_label"][label]
        label_metrics: dict[str, Any] = {}
        for section, name in (
            ("candidate", "recall_at_30"),
            ("final", "hit_at_1"),
            ("final", "hit_at_3"),
            ("locator_accuracy", None),
        ):
            key = f"{section}.{name}" if name else section
            base_value = (
                baseline_summary[section][name] if name else baseline_summary[section]
            )
            candidate_value = (
                candidate_summary[section][name] if name else candidate_summary[section]
            )
            delta = candidate_value - base_value
            label_metrics[key] = {
                "baseline": base_value,
                "candidate": candidate_value,
                "absolute_delta": delta,
                "relative_delta": _relative(delta, base_value),
                "confidence_interval_95": (delta, delta),
            }
        result["by_label"][label] = {
            "case_count": candidate_summary.get("case_count", 0),
            "metrics": label_metrics,
        }
    return result


def render_comparison_markdown(comparison: dict[str, Any]) -> str:
    """渲染 baseline/candidate 的可读比较报告。"""
    lines = [
        "# Retrieval Evaluation Comparison",
        "",
        f"- Baseline: `{comparison.get('baseline', 'unknown')}`",
        f"- Candidate: `{comparison.get('candidate', 'unknown')}`",
        f"- Cases: {comparison.get('case_count', 0)}",
        "",
        "| Metric | Baseline | Candidate | Delta | Relative |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for metric, value in comparison.get("metrics", {}).items():
        relative = value.get("relative_delta")
        relative_text = "n/a" if relative is None else f"{relative:.2%}"
        lines.append(
            f"| {metric} | {value['baseline']:.4f} | {value['candidate']:.4f} | "
            f"{value['absolute_delta']:+.4f} | {relative_text} |"
        )
    return "\n".join(lines) + "\n"
