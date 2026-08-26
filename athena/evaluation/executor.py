"""在准备好的运行时上顺序执行正式评估用例。"""

from __future__ import annotations

from collections.abc import Iterable

from athena.evaluation.models import EvalCase, RetrievalOutcome
from athena.evaluation.runtime import EvaluationRuntime


async def execute_cases(
    runtime: EvaluationRuntime, cases: Iterable[EvalCase]
) -> list[RetrievalOutcome]:
    """按输入顺序串行执行评估用例。

    参数：
        runtime: 已完成准备的评估运行时。
        cases: 要执行的评估用例迭代器。

    返回值：
        与输入顺序一致的检索结果列表。

    异常：
        RuntimeError: 运行时尚未准备好。
        Exception: 单个用例执行时由底层检索服务传播的异常。
    """
    return [await runtime.retrieve(case) for case in cases]
