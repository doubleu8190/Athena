"""将与传输层无关的评估请求值转换为领域配置。
"""

from __future__ import annotations

from typing import Any

from domain.evaluation import EvaluationConfig, EvaluationMode, GateRule


def make_gate_rules(values: list[dict[str, Any]]) -> tuple[GateRule, ...]:
    """从配置值构造评估门槛规则。

    参数：
    - `values`（list[dict[str, Any]]）：待转换或校验的值集合；约束：无额外运行时限制。

    返回：
    - tuple[GateRule, ...]：已校验的评估门槛规则及其对应名称。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return tuple(GateRule(str(item["metric"]), str(item["op"]), float(item["value"]), bool(item.get("required", True))) for item in values)


def make_evaluation_config(value: dict[str, Any], dataset_version: str) -> EvaluationConfig:
    """校验并构造评估运行配置。

    参数：
    - `value`（dict[str, Any]）：待解析、校验或转换的输入值；约束：无额外运行时限制。
    - `dataset_version`（str）：用于筛选或关联评估数据集的版本标识；约束：无额外运行时限制。

    返回：
    - EvaluationConfig：已完成类型转换和参数校验的评估配置。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
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
