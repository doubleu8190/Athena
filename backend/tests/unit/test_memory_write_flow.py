"""覆盖精简后的重试、项目范围和回顾独立性。"""
from dataclasses import replace
import json

import pytest

from application.memory import DefaultMemoryWriteTrigger, LLMFactExtractor, MemoryFactResolver, MemoryWriteWorkflow
from domain.memory import (
    CompletedTurn, MemoryEpisodeProposal, MemoryEpisodeSummary, MemoryExecutionEvidence,
    MemoryTaskOutcomeEvidence, ResolutionAction,
)
from tests.fakes.leases import lease_grant
from tests.fakes.memory_pipeline import Memory, Embedder, Snapshots, candidate, dependencies


@pytest.mark.asyncio
async def test_retry_reuses_extraction_and_skips_committed_candidate_before_resolving():
    """第二个 embedding 失败后重试，首个事实不重写、提取不重复。"""
    class LLM:
        calls = 0
        async def invoke_structured(self, messages, schema):
            self.calls += 1
            return schema.model_validate_json(json.dumps({"atomic_facts": [
                {"key": "timezone", "value": "UTC", "category": "profile", "scope_kind": "global", "confidence": 1.0},
                {"key": "primary_language", "value": "Python", "category": "technical_decision", "scope_kind": "project", "confidence": 1.0},
            ]}))
    class FlakyEmbedder(Embedder):
        failed = False
        async def embed_query(self, text):
            if text.startswith("primary_language") and not self.failed:
                self.failed = True
                raise RuntimeError("embedding temporarily unavailable")
            return await super().embed_query(text)
    memory, llm, embedder = Memory(), LLM(), FlakyEmbedder()
    workflow = MemoryWriteWorkflow(memory, DefaultMemoryWriteTrigger(),
        LLMFactExtractor(llm, snapshots=Snapshots()), MemoryFactResolver(memory), embedder, **dependencies())
    turn = CompletedTurn("turn-1", "session-1", "我的时区 UTC；项目使用 Python", project_id="project-1")
    lease = lease_grant("memory_processing", turn.turn_id)
    with pytest.raises(RuntimeError, match="embedding temporarily unavailable"):
        await workflow.process_completed_turn(turn, lease=lease)
    assert len(memory.items) == 1
    assert await workflow.process_completed_turn(turn, lease=lease) == 1
    assert await workflow.process_completed_turn(turn, lease=lease) == 0
    assert len(memory.items) == 2
    assert llm.calls == 1
    assert len(embedder.items) == 2
    facts = [item.metadata["fact"] for item in memory.items.values()]
    assert {(fact["scope_kind"], fact["scope_id"]) for fact in facts} == {
        ("global", "global"), ("project", "project-1"),
    }


@pytest.mark.asyncio
async def test_fact_failure_does_not_block_episode_or_promote_procedures():
    """事实提取失败仍保存回顾；经验只保留在回顾内，重试复用已有回顾。"""
    class Extractor:
        async def extract(self, turn, *, lease):
            raise ValueError("invalid fact proposal")
    class Summarizer:
        calls = 0
        async def summarize(self, turn):
            self.calls += 1
            return MemoryEpisodeProposal(retain=True,
                summary=MemoryEpisodeSummary(title="部署检查", goal="完成部署", outcome="completed",
                    result="部署检查完成", reusable_learnings=("切流前检查健康状态",)),
                source_ref_ids=(turn.turn_id,))
    memory, summarizer = Memory(), Summarizer()
    dep = dependencies()
    workflow = MemoryWriteWorkflow(memory, DefaultMemoryWriteTrigger(), Extractor(),
        MemoryFactResolver(memory), Embedder(), episodes=dep["episodes"], episode_summarizer=summarizer)
    fact = candidate().fact
    source = replace(fact.source_refs[0], source_type="run")
    evidence = MemoryExecutionEvidence(run_id="turn-1", source_fingerprint="a" * 64,
        total_task_count=1, truncated=False, tasks=(MemoryTaskOutcomeEvidence(task_id="task-1",
            title="部署检查", status="completed", execution_generation=1),), source_refs=(source,))
    turn = CompletedTurn("turn-1", "session-1", "部署", project_id="project-1", execution_evidence=evidence)
    lease = lease_grant("memory_processing", turn.turn_id)
    for _ in range(2):
        with pytest.raises(ValueError, match="invalid fact proposal"):
            await workflow.process_completed_turn(turn, lease=lease)
    episode = await dep["episodes"].get_by_turn(turn.turn_id)
    assert episode.summary.reusable_learnings == ("切流前检查健康状态",)
    assert summarizer.calls == 1
    assert memory.items == {}
    # 命令型输入不提取事实，但不阻止回顾路径。
    assert await workflow.process_completed_turn(replace(turn, user_text="/deploy"), lease=lease) == 0


@pytest.mark.asyncio
async def test_rules_distinguish_corrections_conflicts_and_unknown_keys():
    """明确更正可修订；普通单值冲突保留；未知属性不自动覆盖。"""
    memory = Memory()
    resolver = MemoryFactResolver(memory)
    for key, expected in [("preferred_language", ResolutionAction.CREATE), ("custom_setting", ResolutionAction.SKIP)]:
        old = candidate(key=key)
        resolution = (await resolver.resolve([old]))[0]
        await memory.apply_resolution(resolution, operation_id=f"old-{key}", embedding=[1.0],
            lease=lease_grant("memory_processing", "turn-1"))
        new = candidate("turn-2", value="Rust", key=key)
        conflict = (await resolver.resolve([new]))[0]
        assert conflict.action is expected
        if expected is ResolutionAction.CREATE:
            assert conflict.relation == "contradicts"
        correction = replace(new, fact=replace(new.fact, claim_intent="correct"))
        decision = (await resolver.resolve([correction]))[0]
        assert decision.action is ResolutionAction.REVISE
        assert decision.target_memory_id == f"old-{key}"
