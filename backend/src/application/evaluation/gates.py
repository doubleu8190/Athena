"""评估回归指标和绝对阈值门槛。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from domain.evaluation import GateResult, GateRule, MetricValue


def _compare(actual: float, op: str, expected: float) -> bool:
    """按指定运算符比较实际指标与门槛值。

    参数：
    - `actual`（float）：待与预期值比较的实际值；约束：无额外运行时限制。
    - `op`（str）：执行比较时使用的运算符；约束：无额外运行时限制。
    - `expected`（float）：门槛规则要求达到的预期值；约束：无额外运行时限制。

    返回：
    - bool：操作是否成功或条件是否成立。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
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
    """根据评估规则、运行指标和基线生成门槛结果。

    参数：
    - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
    - `rules`（Iterable[GateRule]）：用于计算评估门槛的规则集合；约束：无额外运行时限制。
    - `metrics`（Iterable[MetricValue]）：评估运行产生的指标集合；约束：无额外运行时限制。
    - `baseline`（Mapping[str, float] | None）：用于比较的基线指标映射；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

    返回：
    - tuple[GateResult, ...]：每条门槛规则对应的通过、失败或缺失指标判定。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
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
