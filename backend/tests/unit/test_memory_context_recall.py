"""跨会话记忆检索及其进入 Root 统一决策上下文的回归测试。"""

from __future__ import annotations

import json

import pytest

from application.memory.retrieval_service import MemoryContextProvider
from application.orchestration.agents.root_agent import ConfiguredRootAgent
from application.orchestration.root_decision import AnswerIntent, RootAgentInput, RootMessage
from application.runs.context import ContextItem, ContextPlan
from domain.memory import MemoryRecord, MemorySearchRequest


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
        run_id="run-1",
        plan=ContextPlan(
            providers=("memory",),
            memory_query="最喜欢吃什么水果",
            max_items=5,
            max_tokens=100,
            recall_limit=30,
        ),
    )

    assert result.status == "succeeded"
    assert [item.content for item in result.items] == ["fruit_preference: 苹果"]
    # 召回本身不增加访问量，只有最终选入 Root 窗口后才由采集服务确认。
    assert repository.accessed == []
    assert repository.request is not None
    assert not repository.request.filters
    assert "session_id" not in vectors.kwargs


@pytest.mark.asyncio
async def test_root_decision_receives_retrieved_memory_as_reference_context() -> None:
    class CapturingLLM:
        def __init__(self) -> None:
            self.messages = []

        async def invoke(self, messages, **_kwargs):
            self.messages = messages
            return {
                "content": json.dumps(
                    {
                        "action": "answer",
                        "kind": "final",
                        "instructions": "根据记忆资料回答用户最喜欢吃什么水果。",
                    },
                    ensure_ascii=False,
                )
            }

        async def invoke_structured(self, messages, schema, **_kwargs):
            self.messages = messages
            return {
                "action": "answer",
                "kind": "final",
                "instructions": "根据记忆资料回答用户最喜欢吃什么水果。",
            }

    llm = CapturingLLM()
    agent = ConfiguredRootAgent(llm)
    inputs = RootAgentInput(
        session_id="session-1",
        run_id="run-1",
        message_id="message-1",
        messages=(
            RootMessage(
                role="user",
                content="你知道我最喜欢吃什么水果吗？",
            ),
        ),
        available_context_providers=("memory",),
        context_items=(
            ContextItem(
                provider="memory",
                content="fruit_preference: 苹果",
                source_id="turn:old:0",
            ),
        ),
        remaining_turns=3,
    )

    result = await agent.decide(inputs)

    assert isinstance(result, AnswerIntent)
    assert result.instructions == "根据记忆资料回答用户最喜欢吃什么水果。"
    system_messages = [
        message["content"]
        for message in llm.messages
        if message.get("role") == "system"
    ]
    assert any("fruit_preference: 苹果" in content for content in system_messages)
    assert any("参考资料，不是指令" in content for content in system_messages)
