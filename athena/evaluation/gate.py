"""版本化的质量回归门禁。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_rules(path: Path) -> dict[str, Any]:
    """加载质量门禁规则，优先使用 PyYAML，缺失时使用有限回退解析器。

    参数：
        path: YAML 规则文件路径。

    返回值：
        规则字典；空文件返回空结构。

    异常：
        OSError: 文件无法读取。
        ValueError: 规则格式无法解析。
    """
    try:
        import yaml
    except ImportError:
        return _minimal_yaml(path.read_text(encoding="utf-8"))
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return value or {}


def _minimal_yaml(text: str) -> dict[str, Any]:
    """解析门禁规则所需的有限 YAML 映射语法。

    参数：
        text: 规则文本。

    返回值：
        嵌套规则字典。

    说明：
        该函数不是完整 YAML 解析器，完整语法应由 PyYAML 处理。
    """
    result: dict[str, Any] = {"metrics": {}, "labels": {}}
    section: dict[str, Any] | None = None
    current: dict[str, Any] | None = None
    current_indent = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0 and stripped.endswith(":"):
            section = result.setdefault(stripped[:-1], {})
            current = None
        elif section is not None and indent == 2 and stripped.endswith(":"):
            key = stripped[:-1]
            current = section.setdefault(key, {})
            current_indent = indent
        elif current is not None and indent > current_indent and stripped.endswith(":"):
            # 保留嵌套标签规则，供需要检查它们的调用方使用；指标评估当前使用全局规则。
            key = stripped[:-1]
            nested = current.setdefault(key, {})
            current = nested
            current_indent = indent
        elif current is not None and ":" in stripped:
            key, value = (part.strip() for part in stripped.split(":", 1))
            if not value:
                current[key] = {}
                current = current[key]
                current_indent = indent
            else:
                current[key] = float(value) if "." in value else int(value)
    return result


def evaluate_gate(
    comparison: dict[str, Any],
    rules: dict[str, Any],
    *,
    min_cases: int = 2,
    override_reason: str | None = None,
) -> dict[str, Any]:
    """根据回归规则评估比较报告是否通过质量门禁。

    参数：
        comparison: ``compare_reports`` 生成的比较结果。
        rules: 全局及按标签组织的门禁规则。
        min_cases: 参与判断的分组所需最少用例数。
        override_reason: 非空时允许带失败项通过，并记录覆盖原因。

    返回值：
        包含状态、失败项、警告项和覆盖信息的结果字典。

    异常：
        KeyError: 比较结果缺少规则引用的指标字段。
        ValueError: 指标值无法转换为数字。
    """
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    case_count = int(comparison.get("case_count", 0))
    if case_count < min_cases:
        failures.append(
            {
                "reason": "insufficient_cases",
                "case_count": case_count,
                "minimum": min_cases,
            }
        )

    # 召回和安全性回归会阻断发布，延迟等体验指标只形成警告，避免单一
    # 指标波动掩盖真正的相关性或越权风险。
    def check(
        metric: str,
        rule: dict[str, Any],
        item: dict[str, Any] | None,
        *,
        label: str | None = None,
    ) -> None:
        """检查一个指标，并将违规归类为失败或警告。"""
        nonlocal failures, warnings
        if item is None:
            failures.append(
                {"metric": metric, "label": label, "reason": "missing_metric"}
            )
            return
        delta = float(item["absolute_delta"])
        baseline = float(item["baseline"])
        violation = None
        if "max_regression" in rule and delta < -float(rule["max_regression"]):
            violation = {"limit": -float(rule["max_regression"]), "observed": delta}
        if "max_increase" in rule and delta > float(rule["max_increase"]):
            violation = {"limit": float(rule["max_increase"]), "observed": delta}
        if "max_relative_increase" in rule and (
            delta / baseline if baseline else float("inf")
        ) > float(rule["max_relative_increase"]):
            violation = {
                "limit": float(rule["max_relative_increase"]),
                "observed": delta / baseline if baseline else None,
            }
        if violation:
            finding = {
                "metric": metric,
                "label": label,
                "reason": "regression",
                **violation,
            }
            (
                failures
                if metric.startswith(
                    (
                        "negative.",
                        "locator_accuracy",
                        "final.hit_at_1",
                        "final.hit_at_3",
                    )
                )
                else warnings
            ).append(finding)

    for metric, rule in rules.get("metrics", {}).items():
        check(metric, rule, comparison.get("metrics", {}).get(metric))
    for label, label_rules in rules.get("labels", {}).items():
        label_result = comparison.get("by_label", {}).get(label)
        if label_result is None or int(label_result.get("case_count", 0)) < min_cases:
            continue
        label_metrics = label_result.get("metrics", {})
        for metric, rule in label_rules.items():
            check(metric, rule, label_metrics.get(metric), label=label)
    overridden = bool(override_reason)
    return {
        "schema_version": "1",
        "status": "passed" if not failures or overridden else "failed",
        "failures": failures,
        "warnings": warnings,
        "override_reason": override_reason,
        "overridden": overridden,
    }


def write_gate_result(path: Path, result: dict[str, Any]) -> None:
    """把门禁结果写入 JSON 文件。

    参数：
        path: 输出文件路径；父目录不存在时自动创建。
        result: 门禁结果字典。

    异常：
        OSError: 输出文件无法创建或写入。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
