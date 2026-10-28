"""记忆流水线测试依赖；真实事务语义由 PostgreSQL 集成测试覆盖。"""
from dataclasses import replace

from domain.memory import (
    FactCardinality, MemoryCandidate, MemoryEpisodeProposal, MemoryFact,
    MemoryRecord, MemoryStatus, MemorySourceReference, MemoryWriteCommand,
    MemoryStateChangedError, ResolutionAction,
)
from shared.contracts.memory import memory_domain_to_schema
from tests.fakes.in_memory import InMemoryMemoryPort, InMemoryMemoryEpisodeRepository


class Snapshots:
    """只保留首次提取清单。"""
    def __init__(self):
        self.items = {}

    async def get_extraction_snapshot(self, turn_id):
        return self.items.get(turn_id)

    async def save_extraction_snapshot(self, snapshot, *, lease):
        return self.items.setdefault(snapshot.turn_id, snapshot)


class Embedder:
    """提供确定的 1024 维向量。"""
    def __init__(self):
        self.items = []

    async def embed_query(self, text):
        self.items.append(text)
        return [1.0] + [0.0] * 1023


class Summarizer:
    """默认不保留任务回顾。"""
    async def summarize(self, turn):
        return MemoryEpisodeProposal(retain=False)


class Memory(InMemoryMemoryPort):
    """为单元测试提供完整提交边界。"""
    async def apply_resolution(self, resolution, *, operation_id, embedding, lease):
        if operation_id in self.items:
            return False
        candidate = resolution.candidate
        existing = await self.list_fact_group(candidate.fact)
        if tuple(sorted(item.id for item in existing)) != resolution.expected_memory_ids:
            raise MemoryStateChangedError('memory_fact_group_changed')
        metadata = {"session_id": candidate.session_id, "source_turn_id": candidate.source_turn_id,
            "type": candidate.memory_type, "category": candidate.category,
            "fact": memory_domain_to_schema(candidate.fact).model_dump(mode="json")}
        if resolution.action is ResolutionAction.REVISE:
            old = self.items[resolution.target_memory_id]
            self.items[old.id] = replace(old, status=MemoryStatus.SUPERSEDED)
        await self.create_memory(MemoryWriteCommand(candidate.content, metadata, operation_key=operation_id))
        if resolution.relation:
            await self.add_relation(operation_id, resolution.target_memory_id,
                resolution.relation, operation_id=operation_id)
        return True


def candidate(
    turn_id="turn-1",
    value="Python",
    key="preferred_language",
    *,
    intent="assert",
    polarity="affirmed",
    cardinality: FactCardinality = FactCardinality.UNKNOWN,
):
    """构造明确个人偏好的结构化事实候选。"""
    observed = "2026-10-28T00:00:00+00:00"
    fact = MemoryFact(scope_kind="global", scope_id="global", subject_key="user:self",
        fact_key=key, fact_value=value, claim_intent=intent, polarity=polarity,
        cardinality=cardinality,
        confidence=1.0, asserted_at=observed,
        source_refs=(MemorySourceReference(source_type="turn", source_id=turn_id,
            excerpt=f"我的偏好是 {value}", observed_at=observed),))
    return MemoryCandidate(f"{key}: {value}", turn_id, session_id="session-1", fact=fact)


def dependencies():
    """提供独立的回顾依赖。"""
    return dict(episode_summarizer=Summarizer(), episodes=InMemoryMemoryEpisodeRepository())
