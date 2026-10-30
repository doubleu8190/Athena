"""内置确定性指标评估器及其注册表。
"""

from __future__ import annotations

from collections.abc import Sequence

from application.evaluation.ports import MetricEvaluator
from domain.evaluation import EvalCase, ExecutionArtifact, MetricValue
from domain.evaluation.metrics import evaluate_answer_rules, evaluate_retrieval_case


class RetrievalMetricsEvaluator(MetricEvaluator):
    """计算检索相关性和候选覆盖等确定性指标。
    """
    name = "retrieval_metrics"
    version = "1"

    async def evaluate(self, case: EvalCase, artifact: ExecutionArtifact, *, run_id: str) -> Sequence[MetricValue]:
        """对单个评估用例计算当前评估器负责的指标。

        参数：
        - `case`（EvalCase）：当前待执行或评估的数据集用例；约束：无额外运行时限制。
        - `artifact`（ExecutionArtifact）：当前用例执行产生的产物，供评估器计算指标；约束：无额外运行时限制。
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。

        返回：
        - Sequence[MetricValue]：由当前评估器计算出的指标值集合。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return evaluate_retrieval_case(case, artifact, run_id=run_id)


class AnswerRulesEvaluator(MetricEvaluator):
    """按评估用例中的规则检查回答结果。
    """
    name = "answer_rules"
    version = "1"

    async def evaluate(self, case: EvalCase, artifact: ExecutionArtifact, *, run_id: str) -> Sequence[MetricValue]:
        """对单个评估用例计算当前评估器负责的指标。

        参数：
        - `case`（EvalCase）：当前待执行或评估的数据集用例；约束：无额外运行时限制。
        - `artifact`（ExecutionArtifact）：当前用例执行产生的产物，供评估器计算指标；约束：无额外运行时限制。
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。

        返回：
        - Sequence[MetricValue]：由当前评估器计算出的指标值集合。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return evaluate_answer_rules(case, artifact, run_id=run_id)


class AgentRulesEvaluator(MetricEvaluator):
    """按评估用例规则检查 Agent 执行产物。
    """
    name = "agent_rules"
    version = "1"

    async def evaluate(self, case: EvalCase, artifact: ExecutionArtifact, *, run_id: str) -> Sequence[MetricValue]:
        """对单个评估用例计算当前评估器负责的指标。

        参数：
        - `case`（EvalCase）：当前待执行或评估的数据集用例；约束：无额外运行时限制。
        - `artifact`（ExecutionArtifact）：当前用例执行产生的产物，供评估器计算指标；约束：无额外运行时限制。
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。

        返回：
        - Sequence[MetricValue]：由当前评估器计算出的指标值集合。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
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
    """创建内置指标评估器映射。

    参数：
    - 无参数。

    返回：
    - dict[str, object]：以评估器名称为键、内置评估器为值的注册表。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return {
        RetrievalMetricsEvaluator.name: RetrievalMetricsEvaluator(),
        AnswerRulesEvaluator.name: AnswerRulesEvaluator(),
        AgentRulesEvaluator.name: AgentRulesEvaluator(),
    }


__all__ = ["AgentRulesEvaluator", "AnswerRulesEvaluator", "RetrievalMetricsEvaluator", "default_evaluators"]
