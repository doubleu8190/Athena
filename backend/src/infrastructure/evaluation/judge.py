"""Structured LLM judge adapter."""

from __future__ import annotations

import json
from typing import Any

from domain.evaluation import EvalCase, ExecutionArtifact, MetricValue


class StructuredJudgeEvaluator:
    name = "groundedness_judge"
    version = "1"

    def __init__(self, provider: Any, *, max_attempts: int = 2) -> None:
        self.provider = provider
        self.max_attempts = max_attempts

    async def evaluate(self, case: EvalCase, artifact: ExecutionArtifact, *, run_id: str) -> tuple[MetricValue, ...]:
        reference = case.answer.reference if case.answer else ""
        prompt = {
            "task": "Score the answer using only the supplied case and evidence.",
            "output_schema": {"correctness": {"score": "0-4"}, "groundedness": {"score": "0-4"}, "completeness": {"score": "0-4"}, "pass": "boolean"},
            "case": dict(case.input),
            "reference": reference,
            "answer": artifact.output_text,
            "evidence": dict(artifact.trace),
        }
        last_error = "judge_parse_error"
        for _ in range(self.max_attempts):
            response = await self.provider.invoke([{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}])
            text = response.get("content", "") if isinstance(response, dict) else getattr(response, "content", str(response))
            try:
                value = json.loads(text) if isinstance(text, str) else text
                scores = {name: float(value[name]["score"]) for name in ("correctness", "groundedness", "completeness")}
                passed = bool(value.get("pass", False))
                return tuple(MetricValue(run_id, f"answer.{name}", score, case_id=case.case_id, value_json={"judge": value}) for name, score in scores.items()) + (MetricValue(run_id, "answer.judge_pass", 1.0 if passed else 0.0, case_id=case.case_id, value_json={"judge": value}),)
            except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                last_error = str(exc)
        return (MetricValue(run_id, "answer.judge_parse_error", 1.0, case_id=case.case_id, value_json={"error": last_error}),)


__all__ = ["StructuredJudgeEvaluator"]
