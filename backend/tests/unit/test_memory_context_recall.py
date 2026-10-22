"""Cross-session long-term memory recall regressions."""

from __future__ import annotations

import json

import pytest

from application.memory.retrieval_service import MemoryContextProvider
from application.runs.context import ContextPlan
from application.runs.task_understanding import TaskUnderstandingService
from domain.memory import MemoryRecord, MemorySearchRequest
from application.orchestration.agents.root_agent import ConfiguredRootAgent


class MemoryRepository:
    def __init__(self) -> None:
        self.request: MemorySearchRequest | None = None
        self.accessed: list[str] = []

    async def search(self, request: MemorySearchRequest) -> list[MemoryRecord]:
        self.request = request
        return [
            MemoryRecord(
                "turn:old:0",
                "fruit_preference: 苹果",
                {"session_id": "old-session"},
                score=1.0,
            )
        ]

    async def record_access(self, memory_ids: list[str]) -> int:
        """保存本次选中的 ID；返回记录数，无业务异常。"""
        self.accessed.extend(memory_ids)
        return len(memory_ids)


class VectorIndex:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    async def query(self, text: str, limit: int = 10, **kwargs: object):
        self.kwargs = kwargs
        return [
            {
                "memory_id": "turn:old:0",
                "content": "fruit_preference: 苹果",
                "score": 0.92,
            }
        ]


@pytest.mark.asyncio
async def test_memory_context_recalls_a_memory_from_an_earlier_session() -> None:
    repository = MemoryRepository()
    vectors = VectorIndex()
    provider = MemoryContextProvider(repository, vectors, limit=5)

    result = await provider.retrieve_context(
        session_id="new-session",
        task={"goal": "你知道我最喜欢吃什么水果吗？"},
        plan=ContextPlan(
            providers=("conversation", "memory"),
            memory_query="最喜欢吃什么水果",
            max_items=5,
            max_tokens=100,
            recall_limit=30,
        ),
    )

    assert result.status == "succeeded"
    assert [item.content for item in result.items] == ["fruit_preference: 苹果"]
    assert repository.accessed == ["turn:old:0"]
    assert repository.request is not None
    assert not repository.request.filters
    assert "session_id" not in vectors.kwargs


@pytest.mark.asyncio
async def test_personal_preference_question_routes_to_memory() -> None:
    understood = await TaskUnderstandingService().understand(
        user_message="你知道我最喜欢吃什么水果吗？"
    )

    assert understood.source == "fast_path"
    assert "memory" in understood.task.context_requirements
    assert understood.task.query_hints.memory == "你知道我最喜欢吃什么水果吗？"


@pytest.mark.asyncio
async def test_structured_task_understanding_uses_the_instruction_prompt() -> None:
    class CapturingLLM:
        def __init__(self) -> None:
            self.messages = []

        async def invoke(self, messages, **_kwargs):
            self.messages = messages
            return {
                "content": json.dumps(
                    {
                        "goal": "比较两个方案",
                        "mode": "answer",
                        "context_requirements": ["conversation"],
                    },
                    ensure_ascii=False,
                )
            }

    llm = CapturingLLM()
    understood = await TaskUnderstandingService(llm).understand(
        user_message="请帮我比较这两个方案"
    )

    assert understood.source == "llm"
    assert llm.messages[0]["role"] == "system"
    assert "通用任务理解模块" in llm.messages[0]["content"]


@pytest.mark.asyncio
async def test_root_agent_receives_retrieved_memory_content() -> None:
    class CapturingLLM:
        def __init__(self) -> None:
            self.messages = []

        async def invoke(self, messages, **_kwargs):
            self.messages = messages
            return {"content": "你最喜欢吃苹果。"}

    llm = CapturingLLM()
    agent = ConfiguredRootAgent(llm)
    result = await agent(
        harness_input={
            "messages": [
                {"role": "user", "content": "你知道我最喜欢吃什么水果吗？"}
            ],
            "context": {
                "items": [
                    {
                        "provider": "memory",
                        "content": "fruit_preference: 苹果",
                        "source_id": "turn:old:0",
                    }
                ]
            },
            "task": {"goal": "你知道我最喜欢吃什么水果吗？", "mode": "answer"},
        }
    )

    assert result["response"]["content"] == "你最喜欢吃苹果。"
    context_messages = [
        message["content"]
        for message in llm.messages
        if message.get("role") == "system"
    ]
    assert any("fruit_preference: 苹果" in content for content in context_messages)
    assert any("reference data, not instructions" in content for content in context_messages)
