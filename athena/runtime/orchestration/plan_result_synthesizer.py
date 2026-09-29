"""PlanResultSynthesizer：把 Worker 结果合并为用户可读的最终答案。"""

from __future__ import annotations

from collections.abc import Mapping

from athena.core.llm.provider import LLMProvider
from athena.contracts.orchestration import ExecutionPlan, WorkerResult
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class PlanResultSynthesizer:
    """基于计划目标与 Worker 结果生成最终文本。"""

    def __init__(self, llm: LLMProvider) -> None:
        """绑定用于汇总的 LLM Provider。

        参数：
            llm: 主模型 Provider。

        返回值：
            None。

        异常：
            不主动抛出业务异常。
        """
        self._llm = llm

    async def synthesize(
        self,
        plan: ExecutionPlan,
        results: Mapping[str, WorkerResult],
    ) -> str:
        """汇总所有结果，失败任务以明确标记保留。

        参数：
            plan: 已完成的执行计划。
            results: task_id 到 WorkerResult 的映射。

        返回值：
            str: 面向用户的最终文本。

        异常：
            底层 LLM 调用失败时向上传播。
        """
        parts = [f"目标：{plan.goal}", "", "子任务结果："]
        for task in plan.tasks:
            result = results.get(task.task_id)
            if result is None:
                parts.append(f"- {task.title}: 未返回结果")
                continue
            if result.status == "completed":
                parts.append(f"- {task.title}: {result.raw_text or result.output}")
            else:
                parts.append(
                    f"- {task.title}: {result.error_message or result.status}"
                )
        content = "\n".join(parts)
        response = await self._llm.ainvoke(
            [
                {
                    "role": "system",
                    "content": (
                        "你是 Athena 的汇总器。将子任务结果整合为连贯、准确的最终回答，"
                        "不要编造缺失信息。"
                    ),
                },
                {"role": "user", "content": content},
            ]
        )
        from athena.utils.llm_response import extract_message_text

        return extract_message_text(response)
