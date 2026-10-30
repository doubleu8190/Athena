"""评估目标、指标计算和进度发布所依赖的应用端口。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from domain.evaluation import (
    EvalCase,
    EvaluationConfig,
    ExecutionArtifact,
    MetricValue,
)


class EvaluationTarget(Protocol):
    """执行单个评估用例的应用端口。
    """
    async def execute(
        self, case: EvalCase, config: EvaluationConfig
    ) -> ExecutionArtifact:
        """在评估目标中执行一个用例，并返回执行产物。

        参数：
        - `case`（EvalCase）：当前待执行或评估的数据集用例；约束：无额外运行时限制。
        - `config`（EvaluationConfig）：当前操作使用的已解析配置；约束：无额外运行时限制。

        返回：
        - ExecutionArtifact：目标系统执行评估用例后产生的输出产物。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


class MetricEvaluator(Protocol):
    """从用例执行产物计算评估指标的应用端口。
    """
    name: str
    version: str

    async def evaluate(
        self,
        case: EvalCase,
        artifact: ExecutionArtifact,
        *,
        run_id: str,
    ) -> Sequence[MetricValue]:
        """根据当前评估器规则为用例计算指标。

        参数：
        - `case`（EvalCase）：当前待执行或评估的数据集用例；约束：无额外运行时限制。
        - `artifact`（ExecutionArtifact）：当前用例执行产生的产物，供评估器计算指标；约束：无额外运行时限制。
        - `run_id`（str）：目标运行的稳定标识，用于关联执行及其记录；约束：无额外运行时限制。

        返回：
        - Sequence[MetricValue]：目标实现针对当前用例返回的指标值集合。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


class EvaluationEventSink(Protocol):
    """发布评估运行进度和生命周期事件的应用端口。
    """
    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        """发布评估进度或生命周期事件。

        参数：
        - `event_type`（str）：要发布的事件类型名称；约束：无额外运行时限制。
        - `payload`（dict[str, Any]）：随事件或消息传递的结构化内容；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        ...


__all__ = ["EvaluationEventSink", "EvaluationTarget", "MetricEvaluator"]
