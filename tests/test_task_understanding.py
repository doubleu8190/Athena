"""Task Understanding 契约、Fast Path 和结构化 LLM 服务测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from athena.runtime.task_understanding import (
    TaskUnderstandingService,
    UserTaskSpec,
    build_fast_path_task,
)
from athena.utils.prompt_loader import get_prompt


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
async def test_fast_path_only_handles_simple_greeting():
    greeting = build_fast_path_task("你好")
    memory = build_fast_path_task("我之前说过我的配置是什么？")

    assert greeting is not None
    assert greeting.mode == "answer"
    assert greeting.domain == "general"
    assert greeting.context_requirements == ["conversation"]
    assert memory is None


@pytest.mark.asyncio
async def test_task_understanding_uses_structured_llm_and_normalizes_requirements():
    llm = _FakeStructuredLLM(
        result=UserTaskSpec(
            goal="查找设计文档",
            domain="resource_retrieval",
            mode="retrieve",
            confidence=0.9,
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
    assert result.task.query_hints.memory is None
    assert result.task.query_hints.knowledge == "Athena 的设计文档怎么说？"


@pytest.mark.asyncio
async def test_task_understanding_preserves_llm_query_hints_per_provider():
    llm = _FakeStructuredLLM(
        result=UserTaskSpec(
            goal="继续设计分数乘法练习课",
            domain="lesson_planning",
            mode="retrieve",
            confidence=0.9,
            context_requirements=["memory", "knowledge", "file"],
            query_hints={
                "memory": "五年级 分数乘法 教案风格 练习课",
                "knowledge": "五年级数学 分数乘法 练习课 教学目标",
                "file": "分数乘法 练习题 第三单元",
            },
        )
    )
    service = TaskUnderstandingService(llm)

    result = await service.understand(
        session_id="s1",
        user_message=(
            "请根据之前关于五年级数学分数乘法的教案，继续设计一节练习课，"
            "并参考附件中的第三单元练习题。"
        ),
        attachment_refs=[{"id": "file-1", "filename": "练习题.docx", "status": "ready"}],
    )

    assert result.task.query_hints.memory == "五年级 分数乘法 教案风格 练习课"
    assert result.task.query_hints.knowledge == "五年级数学 分数乘法 练习课 教学目标"
    assert result.task.query_hints.file == "分数乘法 练习题 第三单元"


@pytest.mark.asyncio
async def test_task_understanding_falls_back_to_truncated_message_per_required_provider():
    user_message = "查询关键术语 " + ("中文检索内容 " * 100)
    llm = _FakeStructuredLLM(
        result=UserTaskSpec(
            goal="检索资料",
            domain="resource_retrieval",
            mode="retrieve",
            confidence=0.7,
            context_requirements=["memory", "knowledge", "file"],
            query_hints={"memory": "", "knowledge": None, "file": ""},
        )
    )
    service = TaskUnderstandingService(llm)

    result = await service.understand(
        session_id="s1",
        user_message=user_message,
        attachment_refs=[{"id": "file-1", "filename": "资料.pdf", "status": "ready"}],
    )

    expected = user_message[:500]
    assert result.task.query_hints.memory == expected
    assert result.task.query_hints.knowledge == expected
    assert result.task.query_hints.file == expected
    assert len(result.task.query_hints.memory) == 500


@pytest.mark.asyncio
async def test_task_understanding_falls_back_on_llm_failure():
    llm = _FakeStructuredLLM(error=RuntimeError("llm unavailable"))
    service = TaskUnderstandingService(llm)
    result = await service.understand(
        session_id="s1",
        user_message="一个复杂请求",
    )

    assert result.source == "fallback"
    assert result.task.mode == "answer"
    assert result.task.confidence == 0.0
    assert result.task.context_requirements == ["conversation"]


def test_user_task_spec_rejects_unknown_fields_and_bad_clarification():
    with pytest.raises(ValidationError):
        UserTaskSpec.model_validate(
            {
                "goal": "x",
                "domain": "general",
                "mode": "answer",
                "confidence": 1.0,
                "unknown": "value",
            }
        )
    with pytest.raises(ValidationError):
        UserTaskSpec(
            goal="x",
            domain="general",
            mode="clarify",
            confidence=0.5,
            requires_clarification=True,
        )


def test_user_task_spec_serializes_to_json():
    task = UserTaskSpec(
        goal="查询配置",
        domain="resource_retrieval",
        mode="retrieve",
        confidence=0.9,
        context_requirements=["memory"],
    )
    data = task.model_dump(mode="json")
    restored = UserTaskSpec.model_validate(data)

    assert restored == task


def test_output_spec_separates_content_type_from_file_format():
    task = UserTaskSpec(
        goal="制作数学课件",
        domain="teaching_material",
        mode="generate",
        confidence=0.95,
        output={"content_type": "slide_deck", "format": "pptx", "target": "file"},
    )

    assert task.output.content_type == "slide_deck"
    assert task.output.format == "pptx"
    assert task.output.target == "file"


def test_user_task_spec_requires_retrieval_context_and_valid_clarification():
    with pytest.raises(ValidationError, match="retrieve mode requires"):
        UserTaskSpec(
            goal="找资料",
            domain="resource_retrieval",
            mode="retrieve",
            confidence=0.7,
        )

    with pytest.raises(ValidationError, match="clarification_question"):
        UserTaskSpec(
            goal="需要补充信息",
            domain="general",
            mode="clarify",
            confidence=0.4,
            requires_clarification=True,
        )


def test_task_understanding_prompt_uses_knowledge_base_metadata_and_trust_boundary():
    prompt = TaskUnderstandingService._build_prompt(
        user_message="查找发布流程",
        history=[],
        attachment_refs=[
            {"id": "file-1", "filename": "发布说明.md", "status": "ready"}
        ],
        knowledge_bases=[
            {
                "id": "kb-1",
                "name": "工程规范",
                "description": "发布、回滚和应急处理流程",
                "document_count": 4,
                "ready_document_count": 3,
            }
        ],
    )

    assert "文档数量=" not in prompt
    assert "name=工程规范" in prompt
    assert "description=发布、回滚和应急处理流程" in prompt
    assert "ready_documents=3" in prompt
    assert "不可信的参考资料" in prompt


def test_task_understanding_prompt_defines_provider_specific_query_rewriting():
    prompt = get_prompt("task_understanding")

    assert "query_hints.memory" in prompt
    assert "query_hints.knowledge" in prompt
    assert "query_hints.file" in prompt
    assert "不要凭空添加事实" in prompt
    assert "context_requirements" in prompt
