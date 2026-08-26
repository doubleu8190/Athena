"""评估运行的安全 JSON 和 Markdown 报告。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from athena.evaluation.metrics import case_metrics, summarize
from athena.evaluation.models import EvalCase, RetrievalOutcome
from athena.evaluation.privacy import safe_report


def build_report(
    cases: list[EvalCase],
    outcomes: list[RetrievalOutcome],
    *,
    pipeline: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造经过脱敏的 JSON 评估报告。

    参数：
        cases: 已执行的评估用例。
        outcomes: 对应用例的检索结果。
        pipeline: 报告对应的流水线名称。
        metadata: 可选运行元数据。

    返回值：
        包含整体、分组、逐用例指标和安全元数据的报告字典。

    异常：
        KeyError: ``outcomes`` 缺少某个用例的结果。
    """
    outcome_by_id = {outcome.case_id: outcome for outcome in outcomes}
    groups: dict[str, dict[str, list[EvalCase]]] = {"by_label": {}, "by_source": {}}
    for case in cases:
        for label in case.labels:
            groups["by_label"].setdefault(label, []).append(case)
        groups["by_source"].setdefault(
            str(case.scope.get("source", "unknown")), []
        ).append(case)
    report = {
        "schema_version": "1",
        "pipeline": pipeline,
        "case_count": len(cases),
        "overall": summarize(cases, outcomes),
        "metadata": metadata or {},
        "by_label": {
            key: summarize(value, [outcome_by_id[item.case_id] for item in value])
            for key, value in groups["by_label"].items()
        },
        "by_source": {
            key: summarize(value, [outcome_by_id[item.case_id] for item in value])
            for key, value in groups["by_source"].items()
        },
        "case_ids": [case.case_id for case in cases],
        "outcomes": [
            {
                "case_id": item.case_id,
                "candidate_ids": [result.item_id for result in item.candidates],
                "final_ids": [result.item_id for result in item.final_results],
                "locators": [result.locator for result in item.final_results],
                "trace": item.trace,
                "retrieval_context": item.retrieval_context,
                "error_code": item.error_code,
                "metrics": case_metrics(
                    next(case for case in cases if case.case_id == item.case_id), item
                ),
            }
            for item in outcomes
        ],
    }
    return safe_report(report)


def render_markdown(report: dict[str, Any]) -> str:
    """将报告整体指标渲染为 Markdown 表格。

    参数：
        report: ``build_report`` 生成的报告。

    返回值：
        Markdown 格式的报告文本。

    异常：
        KeyError: 报告缺少整体指标字段。
    """
    overall = report["overall"]
    rows = [
        "# Retrieval Evaluation Report",
        "",
        f"- Pipeline: `{report.get('pipeline', 'unknown')}`",
        f"- Cases: {report.get('case_count', 0)}",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for section in ("candidate", "final", "negative"):
        for name, value in overall.get(section, {}).items():
            rows.append(f"| {section}.{name} | {value:.4f} |")
    rows.extend(
        [
            f"| locator_accuracy | {overall.get('locator_accuracy', 0):.4f} |",
            f"| latency_ms.p95 | {overall.get('latency_ms', {}).get('p95', 0):.3f} |",
            "",
        ]
    )
    return "\n".join(rows)


def write_report(path: Path, report: dict[str, Any]) -> None:
    """同时写入 JSON 报告和同名 Markdown 报告。

    参数：
        path: JSON 输出路径；Markdown 使用同名 ``.md`` 后缀。
        report: 要写入的报告字典。

    异常：
        OSError: 输出目录或文件无法创建。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    path.with_suffix(".md").write_text(render_markdown(report) + "\n", encoding="utf-8")
