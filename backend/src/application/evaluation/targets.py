"""Deterministic application targets used by API smoke and contract tests."""
from application.evaluation.ports import EvaluationTarget
from domain.evaluation import EvalCase, EvaluationConfig, ExecutionArtifact


class FixtureChatTarget(EvaluationTarget):
    async def execute(self, case: EvalCase, config: EvaluationConfig) -> ExecutionArtifact:
        metadata = dict(case.metadata)
        return ExecutionArtifact(
            output_text=str(metadata.get("output", "")),
            ranking=tuple(str(item) for item in metadata.get("ranking", ())),
            trace=dict(metadata.get("trace") or {}),
            duration_ms=0.0,
        )


__all__ = ["FixtureChatTarget"]
