"""提供用于 API 冒烟和契约验证的确定性评估目标。
"""
from application.evaluation.ports import EvaluationTarget
from domain.evaluation import EvalCase, EvaluationConfig, ExecutionArtifact


class FixtureChatTarget(EvaluationTarget):
    """返回固定对话产物的确定性评估目标。
    """
    async def execute(self, case: EvalCase, config: EvaluationConfig) -> ExecutionArtifact:
        """在目标系统中执行评估用例并返回执行产物。

        参数：
        - `case`（EvalCase）：当前待执行或评估的数据集用例；约束：无额外运行时限制。
        - `config`（EvaluationConfig）：当前操作使用的已解析配置；约束：无额外运行时限制。

        返回：
        - ExecutionArtifact：目标系统执行评估用例后产生的输出产物。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        metadata = dict(case.metadata)
        return ExecutionArtifact(
            output_text=str(metadata.get("output", "")),
            ranking=tuple(str(item) for item in metadata.get("ranking", ())),
            trace=dict(metadata.get("trace") or {}),
            duration_ms=0.0,
        )


__all__ = ["FixtureChatTarget"]
