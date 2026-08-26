"""Command-line runner for deterministic offline retrieval evaluation."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from tests.retrieval_eval.fixtures import FixtureLegacyPipeline
from tests.retrieval_eval.metrics import (
    EvaluationOutcome,
    RelevantItem,
    RetrievalEvalCase,
    build_report,
)


def load_cases(path: Path) -> list[RetrievalEvalCase]:
    """Load and validate JSONL cases with line-numbered errors."""
    cases: list[RetrievalEvalCase] = []
    seen_ids: set[str] = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
            case = RetrievalEvalCase(
                case_id=_required_string(value, "id"),
                query=_required_string(value, "query"),
                source=str(value.get("scope", {}).get("source", "memory")),
                attachment_ids=tuple(value.get("scope", {}).get("attachment_ids", [])),
                query_labels=tuple(value.get("query_labels", [])),
                relevant=tuple(
                    RelevantItem(
                        item_id=_required_string(item, "item_id"),
                        gain=int(item.get("gain", 1)),
                        locator=item.get("locator"),
                    )
                    for item in value.get("relevant", [])
                ),
                must_not_return=tuple(value.get("must_not_return", [])),
                expect_empty=bool(value.get("expect_empty", False)),
                notes=str(value.get("notes", "")),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid case at {path}:{line_number}: {exc}") from exc
        if case.case_id in seen_ids:
            raise ValueError(f"Duplicate case id at {path}:{line_number}: {case.case_id}")
        seen_ids.add(case.case_id)
        cases.append(case)
    if not cases:
        raise ValueError(f"No evaluation cases found in {path}")
    return cases


def _required_string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return result


async def evaluate_cases(
    cases: Sequence[RetrievalEvalCase], pipeline: FixtureLegacyPipeline
) -> list[EvaluationOutcome]:
    """Evaluate cases sequentially for deterministic fixture ordering."""
    return [await pipeline.evaluate(case) for case in cases]


def render_markdown(report: dict[str, Any]) -> str:
    """Render a compact human-readable companion report."""
    overall = report["overall"]
    lines = [
        "# Retrieval Evaluation Report",
        "",
        f"- Pipeline: `{report['pipeline']}`",
        f"- Cases: {report['case_count']}",
        "",
        "## Overall",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for section, metrics in (
        ("candidate", overall["candidate"]),
        ("final", overall["final"]),
        ("negative", overall["negative"]),
    ):
        for name, value in metrics.items():
            lines.append(f"| {section}.{name} | {value:.4f} |")
    lines.extend(
        [
            f"| locator_accuracy | {overall['locator_accuracy']:.4f} |",
            f"| latency_ms.p50 | {overall['latency_ms']['p50']:.3f} |",
            f"| latency_ms.p95 | {overall['latency_ms']['p95']:.3f} |",
            f"| llm.calls | {overall['llm']['calls']} |",
            f"| llm.estimated_tokens | {overall['llm']['estimated_tokens']} |",
            "",
            "## Stage Latency",
            "",
            "| Stage | P50 ms | P95 ms |",
            "| --- | ---: | ---: |",
        ]
    )
    for stage, values in overall["stage_latency_ms"].items():
        lines.append(f"| {stage} | {values['p50']:.3f} | {values['p95']:.3f} |")
    lines.extend(
        [
            "",
            "## By Label",
            "",
            "| Label | Cases | Recall@30 | nDCG@10 | Hit@3 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for label, summary in report["by_label"].items():
        lines.append(
            "| {label} | {count} | {recall:.4f} | {ndcg:.4f} | {hit:.4f} |".format(
                label=label,
                count=summary["case_count"],
                recall=summary["candidate"]["recall_at_30"],
                ndcg=summary["final"]["ndcg_at_10"],
                hit=summary["final"]["hit_at_3"],
            )
        )
    return "\n".join(lines) + "\n"


def compare_reports(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Return selected quality deltas for two reports."""
    metric_paths = (
        ("candidate", "recall_at_30"),
        ("final", "hit_at_3"),
        ("final", "ndcg_at_10"),
        ("final", "precision_at_5"),
        ("negative", "empty_false_positive_rate"),
    )
    return {
        "baseline": baseline.get("pipeline", "unknown"),
        "candidate": candidate.get("pipeline", "unknown"),
        "case_count": candidate.get("case_count", 0),
        "deltas": {
            f"{section}.{name}": (
                candidate["overall"][section][name]
                - baseline["overall"][section][name]
            )
            for section, name in metric_paths
        },
    }


def _write_report(output: Path, report: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output.with_suffix(".md").write_text(render_markdown(report), encoding="utf-8")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.pipeline == "compare":
        if args.baseline is None or args.candidate is None:
            raise ValueError("compare mode requires --baseline and --candidate")
        report = compare_reports(
            json.loads(args.baseline.read_text(encoding="utf-8")),
            json.loads(args.candidate.read_text(encoding="utf-8")),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return report

    cases = load_cases(args.cases)
    # PR-01 has one deterministic fixture pipeline. Later PRs replace planned
    # with the feature-flagged implementation while preserving this runner API.
    pipeline = FixtureLegacyPipeline()
    outcomes = await evaluate_cases(cases, pipeline)
    report = build_report(cases, outcomes)
    report.update(
        {
            "pipeline": args.pipeline,
            "outcomes": [asdict(outcome) for outcome in outcomes],
        }
    )
    _write_report(args.output, report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument(
        "--pipeline", choices=("legacy", "planned", "compare"), required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    args = parser.parse_args(argv)
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
