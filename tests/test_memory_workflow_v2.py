import pytest

from athena.core.memory.contracts import (
    CompletedTurn,
    MemoryCandidate,
    MemoryRelationType,
    MemoryResolution,
)
from athena.core.memory.trigger import MemoryTrigger
from athena.core.memory.workflow import MemoryWriteWorkflow
from athena.core.memory.resolver import MemoryResolver
from athena.core.memory.contracts import ResolutionAction


def test_trigger_skips_obvious_low_value_turns():
    result = MemoryTrigger().evaluate(
        CompletedTurn(turn_id="t1", session_id="s1", user_text="你好")
    )
    assert result.should_extract is False


def test_trigger_allows_weak_semantic_signal():
    result = MemoryTrigger().evaluate(
        CompletedTurn(turn_id="t1", session_id="s1", user_text="我一直都是用 Python 写后端的")
    )
    assert result.should_extract is True


def test_trigger_does_not_drop_long_turn_for_partial_negative_signal():
    result = MemoryTrigger().evaluate(
        CompletedTurn(
            turn_id="t1",
            session_id="s1",
            user_text="请计算一下这个方案的成本，并保留当前配置",
        )
    )
    assert result.should_extract is True


def test_trigger_skips_only_standalone_negative_turn():
    result = MemoryTrigger().evaluate(
        CompletedTurn(turn_id="t1", session_id="s1", user_text="请帮我计算")
    )
    assert result.should_extract is False


@pytest.mark.asyncio
async def test_resolver_creates_when_related_score_is_below_threshold():
    class Memory:
        async def search(self, *args, **kwargs):
            return [{"id": "m1", "content": "旧信息", "score": 0.2}]

    candidate = MemoryCandidate(
        content="新信息", source_turn_id="t1", confidence=0.9
    )
    result = await MemoryResolver(Memory()).resolve([candidate])
    assert result[0].action is ResolutionAction.CREATE


@pytest.mark.asyncio
async def test_resolver_marks_explicit_change_as_supersede():
    class Memory:
        async def search(self, *args, **kwargs):
            return [{"id": "m1", "content": "使用 Python", "score": 0.8}]

    candidate = MemoryCandidate(
        content="后端改为 Rust（was Python）", source_turn_id="t1", confidence=0.9
    )
    result = await MemoryResolver(Memory()).resolve([candidate])
    assert result[0].action is ResolutionAction.SUPERSEDE


@pytest.mark.asyncio
async def test_resolver_uses_llm_only_for_ambiguous_related_memory():
    class LLM:
        async def ainvoke(self, messages):
            return type("Response", (), {"content": '{"action":"SUPERSEDE"}'})()

    class Memory:
        async def search(self, *args, **kwargs):
            return [{"id": "m1", "content": "使用 SQLite", "score": 0.8}]

    candidate = MemoryCandidate(
        content="数据库方案调整为 PostgreSQL", source_turn_id="t1", confidence=0.9
    )
    result = await MemoryResolver(Memory(), llm_provider=LLM()).resolve([candidate])
    assert result[0].action is ResolutionAction.SUPERSEDE
    assert result[0].reason == "llm_resolution"


@pytest.mark.asyncio
async def test_write_workflow_persists_only_create_resolutions():
    class Extractor:
        def __init__(self):
            self.existing = None

        async def extract(self, turn, *, existing_memories):
            self.existing = existing_memories
            return [
                MemoryCandidate(
                    content="用户偏好 Python",
                    category="preference",
                    source_turn_id=turn.turn_id,
                )
            ]

    class Memory:
        def __init__(self):
            self.saved = []
            self.search_calls = 0

        async def search(self, *args, **kwargs):
            self.search_calls += 1
            if self.search_calls == 1:
                return [{"id": "m1", "content": "已有相关偏好", "metadata": {"category": "preference"}}]
            return []

        async def add_memory(self, **kwargs):
            self.saved.append(kwargs)

    memory = Memory()
    extractor = Extractor()
    outcome = await MemoryWriteWorkflow(extractor, memory).process_turn(
        CompletedTurn(turn_id="t1", session_id="s1", user_text="以后用 Python")
    )
    assert outcome.triggered is True
    assert len(memory.saved) == 1
    assert memory.saved[0]["metadata"]["source_turn_id"] == "t1"
    assert "已有相关偏好" in extractor.existing
    assert memory.search_calls == 1


@pytest.mark.parametrize(
    ("relation", "llm_response"),
    [
        (MemoryRelationType.CONTRADICTS, '{"action":"CREATE","relation":"contradicts"}'),
        (MemoryRelationType.SUPPORTS, '{"action":"CREATE","relation":"supports"}'),
    ],
)
@pytest.mark.asyncio
async def test_workflow_persists_create_relations(relation, llm_response):
    class LLM:
        async def ainvoke(self, messages):
            return type("Response", (), {"content": llm_response})()

    class Extractor:
        async def extract(self, turn, *, existing_memories):
            return [MemoryCandidate(content="新证据", source_turn_id=turn.turn_id)]

    class Memory:
        def __init__(self):
            self.relations = []

        async def search(self, *args, **kwargs):
            return [{"id": "old-1", "content": "旧事实", "score": 0.8}]

        async def add_memory(self, **kwargs):
            return "new-1"

        async def relate(self, source_id, target_id, relation_type):
            self.relations.append((source_id, target_id, relation_type))

    memory = Memory()
    resolver = MemoryResolver(memory, llm_provider=LLM())
    outcome = await MemoryWriteWorkflow(
        Extractor(), memory, resolver=resolver
    ).process_turn(CompletedTurn(turn_id="t1", session_id="s1", user_text="新证据"))

    assert outcome.resolutions[0].relation_type is relation
    assert memory.relations == [("new-1", "old-1", relation.value)]


@pytest.mark.asyncio
async def test_workflow_applies_update_and_supersede_actions():
    class Extractor:
        async def extract(self, turn, *, existing_memories):
            return [MemoryCandidate(content="更新后的事实", source_turn_id=turn.turn_id)]

    class Resolver:
        async def resolve(self, candidates, *, related_memories):
            return [
                MemoryResolution(
                    action=ResolutionAction.UPDATE,
                    candidate=candidates[0],
                    target_memory_id="m-update",
                    final_content="更新后的事实",
                )
            ]

    class Memory:
        async def search(self, *args, **kwargs):
            return []

        async def update_memory(self, memory_id, content):
            self.updated = (memory_id, content)
            return True

    memory = Memory()
    await MemoryWriteWorkflow(Extractor(), memory, resolver=Resolver()).process_turn(
        CompletedTurn(turn_id="t1", session_id="s1", user_text="更新")
    )
    assert memory.updated == ("m-update", "更新后的事实")
