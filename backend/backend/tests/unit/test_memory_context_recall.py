"""长期记忆跨会话召回与上下文注入回归测试。"""

from __future__ import annotations

import json

import pytest

from application.memory.retrieval_service import MemoryContextProvider
from application.runs.context import ContextPlan
from application.runs.task_understanding import TaskUnderstandingService
from domain.memory import MemoryRecord, MemorySearchRequest
from infrastructure.orchestration.langgraph.runtime.root_agent import ConfiguredRootAgent


class MemoryRepository:
    def __init__(self) -> None:
        self.request: MemorySearchRequest | None = None

    async def search(self, request: MemorySearchRequest) -> list[MemoryRecord]:
        self.request = request
        return [
            MemoryRecord(
                "memory-old-session",
                "fruit_preference: 苹果",
                {"session_id": "old-session"},
                score=1.0,
            )
        ]


class VectorIndex:
    def __init__(self) -> None:
        self.query_text = ""
        self.limit = 0
        self.kwargs: dict[str, object] = {}

    async def query(self, text: str, limit: int = 10, **kwargs: object):
        self.query_text = text
        self.limit = limit
        self.kwargs = kwargs
        return [
            {
                "memory_id": "memory-old-session",
                "content": "fruit_preference: 苹果",
                "score": 0.92,
            }
        ]


@pytest.mark.asyncio
async def test_memory_context_searches_across_sessions() -> None:
    repository = MemoryRepository()
    vectors = VectorIndex()
    provider = MemoryContextProvider(repository, vectors, limit=5)

    result = await provider.retrieve_context(
        session_id="new-session",
        task={"goal": "你知道我最喜欢吃什么水果吗？"},
        plan=ContextPlan(
            providers=("memory",),
            memory_query="最喜欢的 水果",
            max_items=5,
            max_tokens=100,
            recall_limit=30,
        ),
    )

    assert result.status == "succeeded"
    assert [item.content for item in result.items] == ["fruit_preference: 苹果"]
    assert repository.request is not None
    assert not repository.request.filters
    assert vectors.query_text == "最喜欢的 水果"
    assert "session_id" not in vectors.kwargs


@pytest.mark.asyncio
async def test_preference_question_routes_to_memory() -> None:
    understood = await TaskUnderstandingService().understand(
        user_message="你知道我最喜欢吃什么水果吗？"
    )

    assert understood.source == "fast_path"
    assert understood.task.mode == "retrieve"
    assert understood.task.context_requirements == ("conversation", "memory")
    assert understood.task.query_hints.memory == "你知道我最喜欢吃什么水果吗？"


@pytest.mark.asyncio
async def test_task_understanding_calls_the_structured_prompt() -> None:
    class LLM:
        def __init__(self) -> None:
            self.messages: list[dict[str, str]] = []

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

    llm = LLM()
    understood = await TaskUnderstandingService(llm).understand(
        user_message="请帮我比较这两个方案"
    )

    assert understood.source == "llm"
    assert llm.messages[0]["role"] == "system"
    assert "通用任务理解模块" in llm.messages[0]["content"]
    assert "current_message" in llm.messages[1]["content"]


@pytest.mark.asyncio
async def test_root_agent_receives_retrieved_context_text() -> None:
    class LLM:
        def __init__(self) -> None:
            self.messages: list[dict[str, str]] = []

        async def invoke(self, messages, **_kwargs):
            self.messages = messages
            return {"content": "你最喜欢吃苹果。"}

    llm = LLM()
    agent = ConfiguredRootAgent(llm)
    response = await agent(
        harness_input={
            "messages": [
                {"role": "user", "content": "你知道我最喜欢吃什么水果吗？"}
            ],
            "task": {"mode": "answer"},
            "context": {
                "items": [
                    {
                        "provider": "memory",
                        "content": "fruit_preference: 苹果",
                        "source_id": "memory-old-session",
                    }
                ]
            },
        }
    )

    assert response["response"]["content"] == "你最喜欢吃苹果。"
    system_text = "\n".join(
        item["content"] for item in llm.messages if item["role"] == "system"
    )
    assert "fruit_preference: 苹果" in system_text
    assert "不是指令" in system_text or "not instructions" in system_text
