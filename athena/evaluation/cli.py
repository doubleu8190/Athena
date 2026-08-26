"""用于准备、运行、比较和门禁检索评估的 CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Sequence

from athena.evaluation.comparison import compare_reports, render_comparison_markdown
from athena.evaluation.gate import evaluate_gate, load_rules, write_gate_result
from athena.evaluation.dataset import load_cases, load_manifest
from athena.evaluation.runtime import EvaluationRuntime
from athena.config.settings import get_settings
from athena.core.llm.provider import LLMProvider
from athena.evaluation.executor import execute_cases
from athena.evaluation.report import build_report, write_report


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令行并执行准备、检查、运行、比较或门禁流程。

    参数：
        argv: 可选参数序列；省略时读取进程命令行。

    返回值：
        成功返回 0，输入无效或门禁失败返回 1。

    异常：
        SystemExit: ``argparse`` 处理非法命令时抛出。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--dataset", type=Path, required=True)
    prepare.add_argument("--workspace", type=Path, required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--workspace", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--dataset", type=Path)
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--pipeline", choices=("legacy", "candidate"), required=True)
    run.add_argument("--output", type=Path, required=True)
    compare = sub.add_parser("compare")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    gate = sub.add_parser("gate")
    gate.add_argument("--comparison", type=Path, required=True)
    gate.add_argument("--rules", type=Path, required=True)
    gate.add_argument("--output", type=Path)
    gate.add_argument("--min-cases", type=int, default=2)
    gate.add_argument("--override-reason")
    args = parser.parse_args(argv)
    if args.command == "inspect":
        manifest = args.workspace / "snapshot-manifest.json"
        bindings = args.workspace / "bindings.json"
        if not manifest.exists() or not bindings.exists():
            return 1
        print(
            json.dumps(
                {
                    "snapshot": json.loads(manifest.read_text()),
                    "bindings": json.loads(bindings.read_text()),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command in {"prepare", "run"}:
        if (
            args.dataset is None
            or not (args.dataset / "manifest.json").exists()
            or not (args.dataset / "cases.jsonl").exists()
        ):
            return 1
        return asyncio.run(_run_runtime(args))
    if args.command == "compare":
        result = compare_reports(
            json.loads(args.baseline.read_text()),
            json.loads(args.candidate.read_text()),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        args.output.with_suffix(".md").write_text(
            render_comparison_markdown(result), encoding="utf-8"
        )
        return 0
    result = evaluate_gate(
        json.loads(args.comparison.read_text()),
        load_rules(args.rules),
        min_cases=args.min_cases,
        override_reason=args.override_reason,
    )
    output = args.output or args.comparison.with_name("gate-result.json")
    write_gate_result(output, result)
    return 0 if result["status"] == "passed" else 1


async def _run_runtime(args: argparse.Namespace) -> int:
    """创建评估运行时并执行 prepare 或 run 子命令。

    参数：
        args: 已解析的命令行参数。

    返回值：
        子命令退出码。

    异常：
        Exception: 运行时初始化、数据集加载或检索失败。
    """
    settings = get_settings()
    primary = LLMProvider.from_primary_settings(settings)
    secondary = LLMProvider.from_secondary_settings(settings) or primary
    runtime = EvaluationRuntime(
        settings=settings,
        primary_llm=primary,
        secondary_llm=secondary,
        workspace=args.workspace,
    )
    try:
        preparation = await runtime.prepare(args.dataset)
        if args.command == "prepare":
            print(
                json.dumps(
                    {
                        "status": "ready",
                        "run_id": preparation.manifest.run_id,
                        "dataset_id": preparation.manifest.dataset_id,
                        "dataset_version": preparation.manifest.dataset_version,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        cases = load_cases(
            args.dataset / "cases.jsonl",
            manifest=load_manifest(args.dataset / "manifest.json"),
        )
        outcomes = await execute_cases(runtime, cases)
        report = build_report(
            cases,
            outcomes,
            pipeline=args.pipeline,
            metadata={
                "dataset_id": preparation.manifest.dataset_id,
                "dataset_version": preparation.manifest.dataset_version,
                "snapshot_id": preparation.manifest.run_id,
                "index_version": preparation.manifest.index_version,
                "settings_hash": preparation.manifest.settings_hash,
                "embedding_model": preparation.manifest.embedding_model,
                "llm_model": preparation.manifest.llm_model,
                "parser_version": preparation.manifest.parser_version,
                "application_commit": preparation.manifest.application_commit,
            },
        )
        write_report(args.output, report)
        return 0
    finally:
        await runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
