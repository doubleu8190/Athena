"""显式多 Agent 执行请求的确定性路由测试。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from application.runs.task_understanding import TaskUnderstandingService


class _AnswerLLM:
    """返回普通问答分类，模拟结构化模型忽略计划意图。"""

    def __init__(self) -> None:
        """初始化调用计数。

        参数：无。
        返回：无。
        异常：不主动抛出业务异常。
        """
        self.calls = 0

    async def invoke(self, _messages: list[dict[str, Any]], **_kwargs: Any) -> dict[str, str]:
        """返回普通问答分类。

        参数：_messages 是任务理解提示；其他参数由结构化 Provider 提供。
        返回：包含 answer 模式的 JSON 响应。
        异常：不主动抛出业务异常。
        """
        self.calls += 1
        return {
            "content": json.dumps(
                {
                    "goal": "answer a question",
                    "mode": "answer",
                    "context_requirements": ["conversation"],
                }
            )
        }


@pytest.mark.asyncio
async def test_explicit_multi_agent_execution_uses_plan_fast_path() -> None:
    """用户要求实际运行多 Agent 时，即使模型误分类也进入计划模式。"""
    llm = _AnswerLLM()
    understood = await TaskUnderstandingService(llm).understand(
        user_message=(
            "请实际运行一次多 Agent 计划，把下面两项分成可并行任务并合并结论。"
        )
    )

    assert understood.source == "fast_path"
    assert understood.task.mode == "plan"
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_explicit_worker_task_creation_uses_plan_fast_path() -> None:
    """显式创建 Worker 任务时，不让 LLM 将请求降级为普通问答。"""
    llm = _AnswerLLM()
    understood = await TaskUnderstandingService(llm).understand(
        user_message=(
            '请为本次新 Run 创建唯一一个 Worker 任务（allowed_tools=["exec_shell"]）。'
        )
    )

    assert understood.source == "fast_path"
    assert understood.task.mode == "plan"
    assert understood.task.allowed_tools == ("exec_shell",)
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_discussing_multi_agent_without_execution_stays_with_model() -> None:
    """仅讨论多 Agent 概念时不强制启动执行计划。"""
    llm = _AnswerLLM()
    understood = await TaskUnderstandingService(llm).understand(
        user_message="请解释多 Agent 计划的概念。"
    )

    assert understood.source == "llm"
    assert understood.task.mode == "answer"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_task_understanding_falls_back_when_no_structured_llm_is_configured() -> None:
    """无模型环境仍可使用确定性规则处理普通任务。"""
    understood = await TaskUnderstandingService().understand(
        user_message="请介绍一下 asyncio 的取消机制。"
    )

    assert understood.source == "fallback"
    assert understood.task.mode == "answer"
