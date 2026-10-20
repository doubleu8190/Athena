"""Built-in deterministic evaluator registry."""

from __future__ import annotations

from collections.abc import Sequence

from domain.evaluation import EvalCase, ExecutionArtifact, MetricValue
from domain.evaluation.metrics import evaluate_answer_rules, evaluate_retrieval_case


class RetrievalMetricsEvaluator:
    name = "retrieval_metrics"
    version = "1"

    async def evaluate(self, case: EvalCase, artifact: ExecutionArtifact, *, run_id: str) -> Sequence[MetricValue]:
        return evaluate_retrieval_case(case, artifact, run_id=run_id)


class AnswerRulesEvaluator:
    name = "answer_rules"
    version = "1"

    async def evaluate(self, case: EvalCase, artifact: ExecutionArtifact, *, run_id: str) -> Sequence[MetricValue]:
        return evaluate_answer_rules(case, artifact, run_id=run_id)


class AgentRulesEvaluator:
    name = "agent_rules"
    version = "1"

    async def evaluate(self, case: EvalCase, artifact: ExecutionArtifact, *, run_id: str) -> Sequence[MetricValue]:
        if case.agent is None:
            return ()
        calls = artifact.trace.get("tool_calls", ()) if isinstance(artifact.trace, dict) else ()
        names = [str(item.get("tool_name", item.get("name", ""))) for item in calls if isinstance(item, dict)]
        required = set(case.agent.required_tools)
        forbidden = set(case.agent.forbidden_tools)
        allowed = set(case.agent.allowed_tools)
        required_recall = len(required & set(names)) / len(required) if required else 1.0
        forbidden_rate = len(forbidden & set(names)) / len(forbidden) if forbidden else 0.0
        allowed_rate = sum(1 for name in names if not allowed or name in allowed) / len(names) if names else 1.0
        approvals = artifact.trace.get("approvals", ()) if isinstance(artifact.trace, dict) else ()
        approval_compliance = 1.0 if all(item.get("resolved", False) for item in approvals if isinstance(item, dict)) else 0.0
        return (
            MetricValue(run_id, "agent.required_tool_recall", required_recall, case_id=case.case_id),
            MetricValue(run_id, "agent.forbidden_tool_rate", forbidden_rate, case_id=case.case_id),
            MetricValue(run_id, "agent.allowed_tool_rate", allowed_rate, case_id=case.case_id),
            MetricValue(run_id, "agent.task_success", 1.0 if required_recall == 1.0 and forbidden_rate == 0.0 else 0.0, case_id=case.case_id),
            MetricValue(run_id, "agent.approval_compliance", approval_compliance, case_id=case.case_id),
        )


def default_evaluators() -> dict[str, object]:
    return {
        RetrievalMetricsEvaluator.name: RetrievalMetricsEvaluator(),
        AnswerRulesEvaluator.name: AnswerRulesEvaluator(),
        AgentRulesEvaluator.name: AgentRulesEvaluator(),
    }


__all__ = ["AgentRulesEvaluator", "AnswerRulesEvaluator", "RetrievalMetricsEvaluator", "default_evaluators"]
