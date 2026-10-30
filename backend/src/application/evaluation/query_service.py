"""提供只读评估查询用例。
"""

from __future__ import annotations

from typing import Any

from domain.evaluation.ports import EvaluationRepository
from domain.evaluation import GateRule
from .gates import evaluate_gates


class EvaluationQueryService:
    """提供评估运行、数据集和门槛结果的查询用例。
    """
    def __init__(self, repository: EvaluationRepository) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `repository`（EvaluationRepository）：负责读取或持久化领域数据的仓储依赖；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self.repository = repository

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        """读取运行。

        参数：
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。

        返回：
        - dict[str, Any] | None：评估运行详情字典；运行不存在时返回 None。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        run = await self.repository.get_run(run_id)
        if run is None:
            return None
        metrics = await self.repository.list_metrics(run_id)
        gates = await self.repository.list_gates(run_id) if hasattr(self.repository, "list_gates") else ()
        results = await self.repository.list_case_results(run_id)
        metrics_by_case: dict[str, list[dict[str, Any]]] = {}
        for metric in metrics:
            if metric.case_id is not None:
                metrics_by_case.setdefault(metric.case_id, []).append(self._metric(metric))
        cases = [self._case(item) for item in results]
        for item in cases:
            item["metrics"] = metrics_by_case.get(item["case_id"], item["metrics"])
        return {
            "run_id": run.run_id,
            "dataset_version": run.dataset_version,
            "mode": str(run.mode),
            "status": str(run.status),
            "name": run.name,
            "config": dict(run.config),
            "case_count": run.case_count,
            "completed_count": run.completed_count,
            "failed_count": run.failed_count,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "metrics": [self._metric(item) for item in metrics],
            "gates": [self._gate(item) for item in gates],
            "cases": cases,
        }

    async def list_runs(self, **filters: Any) -> tuple[list[dict[str, Any]], int]:
        """列出运行记录。

        参数：
        - `filters`（Any）：按字段和值筛选查询结果的附加条件；约束：无额外运行时限制。

        返回：
        - tuple[list[dict[str, Any]], int]：按筛选条件分页返回的运行详情列表及其总数。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        values, total = await self.repository.list_runs(**filters)
        return [self._run(item) for item in values], total

    async def list_datasets(self) -> list[dict[str, Any]]:
        """列出可用于评估的数据集及其版本摘要。

        参数：
        - 无参数。

        返回：
        - list[dict[str, Any]]：包含数据集版本和用例统计信息的可用数据集列表。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        values = await self.repository.list_manifests()
        return [{"dataset_name": item.dataset_name, "version_id": item.version_id, "manifest_sha256": item.manifest_sha256, "case_count": item.case_count, "tag_counts": dict(item.tag_counts)} for item in values]

    async def compare(self, run_id: str, baseline_run_id: str) -> dict[str, Any] | None:
        """比较两个评估运行的指标结果。

        参数：
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
        - `baseline_run_id`（str）：作为对照的基线运行标识；约束：无额外运行时限制。

        返回：
        - dict[str, Any] | None：两个运行的指标比较结果；任一运行不存在时返回 None。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        current = await self.repository.list_metrics(run_id)
        baseline = await self.repository.list_metrics(baseline_run_id)
        if await self.repository.get_run(run_id) is None or await self.repository.get_run(baseline_run_id) is None:
            return None
        current_values = {item.metric_name: item.value for item in current if item.case_id is None}
        baseline_values = {item.metric_name: item.value for item in baseline if item.case_id is None}
        names = sorted(set(current_values) | set(baseline_values))
        return {"run_id": run_id, "baseline_run_id": baseline_run_id, "items": [{"metric_name": name, "value": current_values.get(name), "baseline": baseline_values.get(name), "delta": None if current_values.get(name) is None or baseline_values.get(name) is None else current_values[name] - baseline_values[name]} for name in names]}

    async def evaluate_gates(self, run_id: str, rules: tuple[GateRule, ...]) -> list[dict[str, Any]] | None:
        """根据评估规则、运行指标和基线生成门槛结果。

        参数：
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。
        - `rules`（tuple[GateRule, ...]）：用于计算评估门槛的规则集合；约束：无额外运行时限制。

        返回：
        - list[dict[str, Any]] | None：基于指定运行和规则计算的门槛结果；运行不存在时返回 None。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        run = await self.repository.get_run(run_id)
        if run is None:
            return None
        metrics = await self.repository.list_metrics(run_id)
        baseline_values: dict[str, float] = {}
        if run.baseline_run_id:
            baseline = await self.repository.list_metrics(run.baseline_run_id)
            baseline_values = {item.metric_name: float(item.value) for item in baseline if item.case_id is None and item.value is not None}
        results = evaluate_gates(run_id, rules, metrics, baseline=baseline_values)
        for item in results:
            await self.repository.save_gate(item)
        return [self._gate(item) for item in results]

    @staticmethod
    def _run(run) -> dict[str, Any]:
        """将评估运行记录转换为查询响应字段。

        参数：
        - `run`（EvalRun）：需要转换为查询响应或报告内容的评估运行；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：包含运行身份、状态和计数的查询字段映射。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return {"run_id": run.run_id, "dataset_version": run.dataset_version, "mode": str(run.mode), "status": str(run.status), "name": run.name, "case_count": run.case_count, "completed_count": run.completed_count, "failed_count": run.failed_count, "started_at": run.started_at, "completed_at": run.completed_at}

    @staticmethod
    def _metric(item) -> dict[str, Any]:
        """将评估指标转换为查询响应字段。

        参数：
        - `item`（MetricValue）：当前待处理的单个条目；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：包含指标名称、数值和样本信息的查询字段映射。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return {"metric_id": item.metric_name, "metric_name": item.metric_name, "case_id": item.case_id, "value": item.value, "value_json": dict(item.value_json), "evidence": list(item.evidence)}

    @staticmethod
    def _gate(item) -> dict[str, Any]:
        """将门槛判定转换为查询响应字段。

        参数：
        - `item`（GateResult）：当前待处理的单个条目；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：包含门槛规则、实际值和判定状态的查询字段映射。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return {"metric": item.metric, "status": item.status, "actual": item.actual, "baseline": item.baseline, "op": item.rule.op, "threshold": item.rule.value, "reason": item.reason}

    @staticmethod
    def _case(item) -> dict[str, Any]:
        """将用例结果转换为查询响应字段。

        参数：
        - `item`（CaseResult）：当前待处理的单个条目；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：包含用例状态、尝试次数、产物和指标的查询字段映射。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return {"case_id": item.case_id, "status": item.status, "attempt": item.attempt.attempt_number, "error": item.error_message, "output": item.attempt.artifact.output_text, "ranking": list(item.attempt.artifact.ranking), "retrieval_run_id": item.attempt.artifact.retrieval_run_id, "agent_run_id": item.attempt.artifact.agent_run_id, "duration_ms": item.attempt.artifact.duration_ms, "metrics": [EvaluationQueryService._metric(metric) for metric in item.metrics]}


__all__ = ["EvaluationQueryService"]
