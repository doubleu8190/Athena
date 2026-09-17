"""Task Understanding 契约、Fast Path 和结构化 LLM 服务测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from athena.runtime.task_understanding import (
    TaskUnderstandingService,
    UserTaskSpec,
    build_fast_path_task,
)


class _FakeStructuredLLM:
    def __init__(self, *, result: UserTaskSpec | None = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls = 0

    async def generate(self, schema, system_prompt: str, user_message: str):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result


@pytest.mark.asyncio
async def test_fast_path_greeting_and_memory():
    greeting = build_fast_path_task("你好")
    memory = build_fast_path_task("我之前说过我的配置是什么？")

    assert greeting is not None
    assert greeting.task_type == "answer"
    assert greeting.context_requirements == ["conversation"]
    assert memory is not None
    assert memory.task_type == "retrieve"
    assert memory.context_requirements == ["memory", "conversation"]


@pytest.mark.asyncio
async def test_task_understanding_uses_structured_llm_and_normalizes_requirements():
    llm = _FakeStructuredLLM(
        result=UserTaskSpec(
            goal="查找设计文档",
            task_type="retrieve",
            context_requirements=["knowledge", "conversation", "knowledge"],
        )
    )
    service = TaskUnderstandingService(llm)
    result = await service.understand(
        session_id="s1",
        user_message="Athena 的设计文档怎么说？",
        attachment_refs=[{"id": "file-1", "filename": "a.pdf", "status": "ready"}],
    )

    assert llm.calls == 1
    assert result.source == "llm"
    assert result.task.context_requirements == ["knowledge", "conversation", "file"]
    assert result.task.query_hints.memory == "Athena 的设计文档怎么说？"


@pytest.mark.asyncio
async def test_task_understanding_falls_back_on_llm_failure():
    llm = _FakeStructuredLLM(error=RuntimeError("llm unavailable"))
    service = TaskUnderstandingService(llm)
    result = await service.understand(
        session_id="s1",
        user_message="一个复杂请求",
    )

    assert result.source == "fallback"
    assert result.task.task_type == "answer"
    assert result.task.context_requirements == ["conversation"]


def test_user_task_spec_rejects_unknown_fields_and_bad_clarification():
    with pytest.raises(ValidationError):
        UserTaskSpec.model_validate(
            {
                "goal": "x",
                "task_type": "answer",
                "unknown": "value",
            }
        )
    with pytest.raises(ValidationError):
        UserTaskSpec(
            goal="x",
            task_type="answer",
            requires_clarification=True,
        )


def test_user_task_spec_serializes_to_json():
    task = UserTaskSpec(
        goal="查询配置",
        task_type="retrieve",
        context_requirements=["memory"],
    )
    data = task.model_dump(mode="json")
    restored = UserTaskSpec.model_validate(data)

    assert restored == task
