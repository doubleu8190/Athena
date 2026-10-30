"""生成格式稳定的 JSON 和 Markdown 评估报告。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from domain.evaluation import CaseResult, EvalRun, GateResult, MetricValue


def _metric_dict(metric: MetricValue) -> dict[str, Any]:
    """将评估指标转换为报告使用的字典。

    参数：
    - `metric`（MetricValue）：当前评估指标；约束：无额外运行时限制。

    返回：
    - dict[str, Any]：包含指标名称、数值、样本、证据和结构化值的报告字段。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return {"metric_name": metric.metric_name, "value": metric.value, "case_id": metric.case_id, "value_json": dict(metric.value_json), "evidence": list(metric.evidence)}


def write_report(output_root: str | Path, run: EvalRun, results: list[CaseResult], metrics: list[MetricValue], gates: list[GateResult]) -> Path:
    """将评估运行、用例结果、指标和门槛写入报告文件。

    参数：
    - `output_root`（str | Path）：报告文件的输出根目录；约束：无额外运行时限制。
    - `run`（EvalRun）：需要转换为查询响应或报告内容的评估运行；约束：无额外运行时限制。
    - `results`（list[CaseResult]）：本次处理产生的结果集合；约束：无额外运行时限制。
    - `metrics`（list[MetricValue]）：评估运行产生的指标集合；约束：无额外运行时限制。
    - `gates`（list[GateResult]）：根据指标判定运行是否通过的门槛结果集合；约束：无额外运行时限制。

    返回：
    - Path：本次运行报告所在的目录路径；目录中包含 JSON、Markdown 和逐用例 JSONL 文件。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    directory = Path(output_root) / run.run_id
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "run": {"run_id": run.run_id, "dataset_version": run.dataset_version, "mode": str(run.mode), "status": str(run.status), "name": run.name, "config": dict(run.config), "case_count": run.case_count, "completed_count": run.completed_count, "failed_count": run.failed_count},
        "metrics": [_metric_dict(metric) for metric in metrics],
        "gates": [{"metric": gate.metric, "status": gate.status, "actual": gate.actual, "expected": gate.rule.value, "op": gate.rule.op, "reason": gate.reason} for gate in gates],
        "cases": [{"case_id": result.case_id, "status": result.status, "attempt": result.attempt.attempt_number, "error": result.error_message, "output": result.attempt.artifact.output_text, "retrieval_run_id": result.attempt.artifact.retrieval_run_id, "metrics": [_metric_dict(metric) for metric in result.metrics]} for result in results],
    }
    (directory / "report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = [f"# Evaluation {run.run_id}", "", f"- Status: `{run.status}`", f"- Dataset: `{run.dataset_version}`", f"- Cases: {run.completed_count}/{run.case_count}", "", "## Metrics", "", "| Metric | Value | Samples |", "| --- | ---: | ---: |"]
    for metric in metrics:
        if metric.case_id is None:
            lines.append(f"| `{metric.metric_name}` | {metric.value if metric.value is not None else '—'} | {metric.value_json.get('sample_count', '—')} |")
    lines.extend(["", "## Gates", "", "| Metric | Status | Reason |", "| --- | --- | --- |"])
    for gate in gates:
        lines.append(f"| `{gate.metric}` | {gate.status} | {gate.reason or ''} |")
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (directory / "case-results.jsonl").write_text("\n".join(json.dumps(item, ensure_ascii=False, default=str) for item in payload["cases"]) + "\n", encoding="utf-8")
    return directory


__all__ = ["write_report"]
