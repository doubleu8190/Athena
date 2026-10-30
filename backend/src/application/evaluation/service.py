"""为 CLI 和 HTTP 适配器提供共用的评估运行编排。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from domain.evaluation import (
    CaseAttempt,
    CaseResult,
    DatasetManifest,
    EvalRun,
    EvaluationConfig,
    EvaluationStatus,
    GateResult,
    MetricValue,
    ExecutionArtifact,
)
from domain.evaluation.ports import EvaluationRepository
from shared import new_id

from .evaluators import default_evaluators
from .gates import evaluate_gates
from .ports import EvaluationEventSink, EvaluationTarget, MetricEvaluator


def _now() -> datetime:
    """生成当前 UTC 时间。

    参数：
    - 无参数。

    返回：
    - datetime：当前带时区的 UTC 时间。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return datetime.now(timezone.utc)


class EvaluationService:
    """创建、排队并执行评估运行，汇总用例和指标结果。
    """
    def __init__(
        self,
        repository: EvaluationRepository,
        *,
        target: EvaluationTarget,
        evaluators: Mapping[str, MetricEvaluator] | None = None,
        event_sink: EvaluationEventSink | None = None,
    ) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `repository`（EvaluationRepository）：负责读取或持久化领域数据的仓储依赖；约束：无额外运行时限制。
        - `target`（EvaluationTarget）：执行待评估用例的目标系统或执行器；约束：无额外运行时限制。
        - `evaluators`（Mapping[str, MetricEvaluator] | None）：按名称索引的指标评估器集合；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `event_sink`（EvaluationEventSink | None）：发布评估生命周期事件的可选依赖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self.repository = repository
        self.target = target
        self.evaluators = dict(evaluators or default_evaluators())
        self.event_sink = event_sink

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        """向可选事件接收端发布评估进度事件。

        参数：
        - `event_type`（str）：要发布的事件类型名称；约束：无额外运行时限制。
        - `payload`（dict[str, Any]）：随事件或消息传递的结构化内容；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        if self.event_sink is not None:
            try:
                await self.event_sink.publish(event_type, payload)
            except Exception:
                # 进度事件仅用于观察；事件存储不可用时，不应把已完成的评估标记为失败。
                return

    def build_run(self, manifest: DatasetManifest, config: EvaluationConfig, *, run_id: str | None = None, status: EvaluationStatus = EvaluationStatus.QUEUED) -> EvalRun:
        """按评估清单和配置构造评估运行记录。

        参数：
        - `manifest`（DatasetManifest）：包含数据集版本、用例和统计信息的清单；约束：无额外运行时限制。
        - `config`（EvaluationConfig）：当前操作使用的已解析配置；约束：无额外运行时限制。
        - `run_id`（str | None）：目标运行的稳定标识，用于关联执行及其记录；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `status`（EvaluationStatus）：当前操作或记录的状态值；约束：可省略，默认值为 `EvaluationStatus.QUEUED`。

        返回：
        - EvalRun：评估运行标识、状态、配置摘要和用例数量。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return EvalRun(
            run_id=run_id or new_id("eval"),
            dataset_version=manifest.version_id,
            mode=config.mode,
            status=status,
            name=config.name,
            config={"target": dict(config.target), "evaluators": list(config.evaluators), "gates": [{"metric": rule.metric, "op": rule.op, "value": rule.value, "required": rule.required} for rule in config.gates]},
            baseline_run_id=config.baseline_run_id,
            case_count=manifest.case_count,
        )

    async def queue(self, manifest: DatasetManifest, config: EvaluationConfig, *, run_id: str | None = None) -> EvalRun:
        """创建评估运行并提交后台执行。

        参数：
        - `manifest`（DatasetManifest）：包含数据集版本、用例和统计信息的清单；约束：无额外运行时限制。
        - `config`（EvaluationConfig）：当前操作使用的已解析配置；约束：无额外运行时限制。
        - `run_id`（str | None）：目标运行的稳定标识，用于关联执行及其记录；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - EvalRun：评估运行标识、状态、配置摘要和用例数量。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        run = self.build_run(manifest, config, run_id=run_id)
        await self.repository.create_run(run)
        await self._publish("evaluation.started", {"evaluation_run_id": run.run_id, "case_count": manifest.case_count, "status": "queued"})
        return run

    async def run(self, manifest: DatasetManifest, config: EvaluationConfig, *, run_id: str | None = None) -> EvalRun:
        """执行评估运行并汇总用例、指标及门槛结果。

        参数：
        - `manifest`（DatasetManifest）：包含数据集版本、用例和统计信息的清单；约束：无额外运行时限制。
        - `config`（EvaluationConfig）：当前操作使用的已解析配置；约束：无额外运行时限制。
        - `run_id`（str | None）：目标运行的稳定标识，用于关联执行及其记录；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - EvalRun：评估运行标识、状态、配置摘要和用例数量。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        run_id = run_id or new_id("eval")
        existing = await self.repository.get_run(run_id)
        # 相同运行标识用于恢复已有运行；首次执行创建记录，恢复执行更新原记录。
        run = replace(self.build_run(manifest, config, run_id=run_id, status=EvaluationStatus.RUNNING), started_at=_now())
        if existing is None:
            await self.repository.create_run(run)
        else:
            await self.repository.update_run(run)
        await self._publish("evaluation.started", {"evaluation_run_id": run_id, "case_count": manifest.case_count})
        # 并发上限由信号量统一控制，每个用例独立记录重试和失败状态。
        semaphore = asyncio.Semaphore(config.concurrency)
        completed = 0
        failed = 0
        results: list[CaseResult] = []

        async def execute_case(case) -> CaseResult:
            """执行用例。

            参数：
            - `case`（EvalCase）：当前待执行或评估的数据集用例；约束：无额外运行时限制。

            返回：
            - CaseResult：单个评估用例的执行结果、指标输入和错误信息。

            异常：
            - 无预期业务异常；依赖调用产生的异常按当前实现传播。
            """
            nonlocal completed, failed
            async with semaphore:
                current_run = await self.repository.get_run(run_id)
                if current_run is not None and str(current_run.status) == EvaluationStatus.CANCELLED:
                    cancelled_attempt = CaseAttempt(new_id("attempt"), run_id, case.case_id, 0, "cancelled", ExecutionArtifact(error_message="cancelled"), _now(), dict(case.input))
                    result = CaseResult(run_id, case.case_id, "cancelled", cancelled_attempt, (), "cancelled")
                    await self.repository.save_case_result(result)
                    return result
                await self._publish("evaluation.case_started", {"evaluation_run_id": run_id, "case_id": case.case_id})
                last_artifact = None
                last_error = None
                attempt_number = 0
                # 超时和目标异常只使当前尝试失败；达到重试上限后再进入指标判定。
                for attempt_number in range(1, config.max_attempts + 1):
                    try:
                        last_artifact = await asyncio.wait_for(self.target.execute(case, config), config.timeout_seconds)
                        last_error = None
                        break
                    except asyncio.TimeoutError:
                        last_artifact = ExecutionArtifact(error_message="timeout")
                        last_error = "timeout"
                    except Exception as exc:  # 目标执行失败记入用例结果，不中断整次评估。
                        last_artifact = ExecutionArtifact(error_message=str(exc))
                        last_error = str(exc)
                    if attempt_number < config.max_attempts:
                        failed_attempt = CaseAttempt(new_id("attempt"), run_id, case.case_id, attempt_number, "retrying", last_artifact, _now(), dict(case.input))
                        await self.repository.save_case_result(CaseResult(run_id, case.case_id, "retrying", failed_attempt, (), last_error))
                assert last_artifact is not None
                attempt = CaseAttempt(new_id("attempt"), run_id, case.case_id, attempt_number, "failed" if last_error else "completed", last_artifact, _now(), dict(case.input))
                metrics: list[MetricValue] = []
                if not last_error:
                    # 仅对成功产物运行评估器，首个缺失或失败的评估器会终止该用例的指标计算。
                    for evaluator_name in config.evaluators:
                        evaluator = self.evaluators.get(evaluator_name)
                        if evaluator is None:
                            last_error = f"unknown evaluator: {evaluator_name}"
                            break
                        try:
                            metrics.extend(await evaluator.evaluate(case, last_artifact, run_id=run_id))
                        except Exception as exc:
                            last_error = f"evaluator {evaluator_name} failed: {exc}"
                            break
                status = "failed" if last_error else "completed"
                result = CaseResult(run_id, case.case_id, status, attempt, tuple(metrics), last_error)
                await self.repository.save_case_result(result)
                for metric in metrics:
                    await self.repository.save_metric(metric)
                completed += 1
                failed += int(bool(last_error))
                await self._publish("evaluation.case_failed" if last_error else "evaluation.case_completed", {"evaluation_run_id": run_id, "case_id": case.case_id, "completed_count": completed, "case_count": manifest.case_count, "error": last_error})
                return result

        results = list(await asyncio.gather(*(execute_case(case) for case in manifest.cases)))
        current_run = await self.repository.get_run(run_id)
        if current_run is not None and str(current_run.status) == EvaluationStatus.CANCELLED:
            return current_run
        all_metrics = [metric for result in results for metric in result.metrics]
        from domain.evaluation.metrics import aggregate_metrics
        # 门槛优先使用聚合指标；单项门槛失败会把运行整体标为失败。
        aggregates = aggregate_metrics(all_metrics)
        for metric in aggregates:
            await self.repository.save_metric(metric)
        baseline_values: dict[str, float] = {}
        if config.baseline_run_id:
            baseline_values = {metric.metric_name: metric.value for metric in await self.repository.list_metrics(config.baseline_run_id) if metric.case_id is None and metric.value is not None}
        gates = evaluate_gates(run_id, config.gates, aggregates, baseline=baseline_values)
        for gate in gates:
            await self.repository.save_gate(gate)
        status = EvaluationStatus.FAILED if failed == len(results) and results else EvaluationStatus.PARTIAL if failed else EvaluationStatus.PASSED
        if any(gate.status == "failed" for gate in gates):
            status = EvaluationStatus.FAILED
        run = replace(run, status=status, completed_count=completed, failed_count=failed, completed_at=_now())
        await self.repository.update_run(run)
        await self._publish("evaluation.completed" if status != EvaluationStatus.FAILED else "evaluation.failed", {"evaluation_run_id": run_id, "status": status, "completed_count": completed, "failed_count": failed})
        return run


__all__ = ["EvaluationService"]
