"""覆盖精简后的重试、项目范围和回顾独立性。"""
from dataclasses import replace
import json

import pytest

from application.memory import LLMFactExtractor, MemoryFactResolver, MemoryWriteWorkflow
from domain.memory import (
    CompletedTurn, FactCardinality, MemoryEpisodeProposal, MemoryEpisodeSummary,
    MemoryExecutionEvidence, MemoryTaskOutcomeEvidence, ResolutionAction,
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
                {"key": "timezone", "value": "UTC", "category": "profile", "scope_kind": "global", "cardinality": "single", "confidence": 1.0},
                {"key": "primary_language", "value": "Python", "category": "technical_decision", "scope_kind": "project", "cardinality": "single", "confidence": 1.0},
            ]}))
    class FlakyEmbedder(Embedder):
        failed = False
        async def embed_query(self, text):
            if text.startswith("primary_language") and not self.failed:
                self.failed = True
                raise RuntimeError("embedding temporarily unavailable")
            return await super().embed_query(text)
    memory, llm, embedder = Memory(), LLM(), FlakyEmbedder()
    workflow = MemoryWriteWorkflow(memory,
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
    workflow = MemoryWriteWorkflow(memory, Extractor(),
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
    # 命令形式也直接进入提取；提取失败仍复用已保存的回顾。
    with pytest.raises(ValueError, match="invalid fact proposal"):
        await workflow.process_completed_turn(replace(turn, user_text="/deploy"), lease=lease)
    assert summarizer.calls == 1


@pytest.mark.asyncio
async def test_declared_cardinality_controls_conflict_resolution_for_any_key():
    """任意事实键都按候选基数决策，声明冲突时保守跳过。"""
    memory = Memory()
    resolver = MemoryFactResolver(memory)
    old_single = candidate(
        key="custom_environment", value="staging",
        cardinality=FactCardinality.SINGLE,
    )
    resolution = (await resolver.resolve([old_single]))[0]
    await memory.apply_resolution(
        resolution,
        operation_id="old-single",
        embedding=[1.0],
        lease=lease_grant("memory_processing", "turn-1"),
    )

    new_single = candidate(
        "turn-2", value="production", key="custom_environment",
        cardinality=FactCardinality.SINGLE,
    )
    conflict = (await resolver.resolve([new_single]))[0]
    assert conflict.action is ResolutionAction.CREATE
    assert conflict.relation == "contradicts"

    correction = replace(
        new_single, fact=replace(new_single.fact, claim_intent="correct")
    )
    decision = (await resolver.resolve([correction]))[0]
    assert decision.action is ResolutionAction.REVISE
    assert decision.target_memory_id == "old-single"

    old_set_member = candidate(
        "turn-3", value="Python", key="custom_tools",
        cardinality=FactCardinality.SET,
    )
    old_set_resolution = (await resolver.resolve([old_set_member]))[0]
    await memory.apply_resolution(
        old_set_resolution,
        operation_id="old-set-member",
        embedding=[1.0],
        lease=lease_grant("memory_processing", "turn-3"),
    )
    new_set_member = candidate(
        "turn-4", value="Rust", key="custom_tools",
        cardinality=FactCardinality.SET,
    )
    set_decision = (await resolver.resolve([new_set_member]))[0]
    assert set_decision.action is ResolutionAction.CREATE
    assert set_decision.relation is None

    inconsistent = candidate(
        "turn-5", value="Go", key="custom_tools",
        cardinality=FactCardinality.SINGLE,
    )
    inconsistent_decision = (await resolver.resolve([inconsistent]))[0]
    assert inconsistent_decision.action is ResolutionAction.SKIP
    assert inconsistent_decision.reason_code == "fact_cardinality_disagreement"

    unknown_old = candidate(
        "turn-6", value="blue", key="custom_setting",
        cardinality=FactCardinality.UNKNOWN,
    )
    unknown_resolution = (await resolver.resolve([unknown_old]))[0]
    await memory.apply_resolution(
        unknown_resolution,
        operation_id="old-unknown",
        embedding=[1.0],
        lease=lease_grant("memory_processing", "turn-6"),
    )
    unknown_new = candidate(
        "turn-7", value="green", key="custom_setting",
        cardinality=FactCardinality.UNKNOWN,
    )
    unknown_decision = (await resolver.resolve([unknown_new]))[0]
    assert unknown_decision.action is ResolutionAction.SKIP
    assert unknown_decision.reason_code == "fact_cardinality_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "cardinality", "intent", "action", "relation"),
    [
        ("Python", FactCardinality.SET, "assert", ResolutionAction.REVISE, "supersedes"),
        ("Rust", FactCardinality.SET, "assert", ResolutionAction.CREATE, None),
        ("Rust", FactCardinality.SET, "correct", ResolutionAction.REVISE, "supersedes"),
        ("Rust", FactCardinality.SINGLE, "assert", ResolutionAction.CREATE, "contradicts"),
        ("Rust", FactCardinality.SINGLE, "correct", ResolutionAction.REVISE, "supersedes"),
    ],
)
async def test_unknown_cardinality_can_be_resolved_by_later_evidence(
    value, cardinality, intent, action, relation
) -> None:
    """明确基数可处理未知事实，且不把集合追加误当成替换。"""
    memory = Memory()
    resolver = MemoryFactResolver(memory)
    old = candidate(key="custom_tools", cardinality=FactCardinality.UNKNOWN)
    await memory.apply_resolution(
        (await resolver.resolve([old]))[0],
        operation_id="unknown-old",
        embedding=[1.0],
        lease=lease_grant("memory_processing", "turn-1"),
    )
    new = candidate(
        "turn-2", value=value, key="custom_tools", intent=intent, cardinality=cardinality
    )
    resolution = (await resolver.resolve([new]))[0]
    assert resolution.action is action
    assert resolution.relation == relation
    if relation is not None:
        assert resolution.target_memory_id == "unknown-old"
    await memory.apply_resolution(
        resolution,
        operation_id="known-new",
        embedding=[1.0],
        lease=lease_grant("memory_processing", "turn-2"),
    )
    if cardinality is FactCardinality.SET and intent == "assert" and value == "Rust":
        later = candidate(
            "turn-3", value="Go", key="custom_tools", cardinality=FactCardinality.SET
        )
        assert (await resolver.resolve([later]))[0].action is ResolutionAction.CREATE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_text",
    ["/deploy", "/Users/example/project 使用 PostgreSQL", "项目说明" * 5_001],
    ids=["command", "absolute-path", "long-message"],
)
async def test_completed_turn_enters_extraction_without_text_filter(user_text: str) -> None:
    """斜杠开头和超过两万字符的输入都交给提取模型判断。"""
    class LLM:
        def __init__(self) -> None:
            """记录模型实际收到的完整提示词。"""
            self.prompts: list[str] = []

        async def invoke_structured(self, messages, schema):
            """返回空候选，表示本轮没有需要持久化的事实。"""
            self.prompts.append(messages[0]["content"])
            return schema.model_validate_json('{"atomic_facts":[]}')

    llm = LLM()
    memory = Memory()
    snapshots = Snapshots()
    workflow = MemoryWriteWorkflow(
        memory,
        LLMFactExtractor(llm, snapshots=snapshots),
        MemoryFactResolver(memory),
        Embedder(),
        **dependencies(),
    )
    turn = CompletedTurn("turn-1", "session-1", user_text, project_id="project-1")
    assert await workflow.process_completed_turn(
        turn, lease=lease_grant("memory_processing", turn.turn_id)
    ) == 0
    assert len(llm.prompts) == 1
    assert user_text in llm.prompts[0]
    assert (await snapshots.get_extraction_snapshot(turn.turn_id)).candidates == ()
