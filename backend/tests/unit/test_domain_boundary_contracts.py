from __future__ import annotations

import pytest

from domain.memory import (
    MemoryCandidateSnapshot,
    MemoryEpisodeProposal,
    MemoryEpisodeSummary,
    MemoryExecutionEvidence,
    MemoryFact,
    MemoryFactExtractionSnapshot,
    MemoryRecordMetadata,
    MemorySourceReference,
    MemoryTaskOutcomeEvidence,
)
from domain.orchestration import (
    WorkerExecutionMessageContent,
    WorkerToolCallContent,
    WorkerToolResultContent,
)
from domain.retrieval.trace_contracts import (
    RetrievalCandidateTrace,
    RetrievalChannelTrace,
    RetrievalHitTrace,
    RetrievalProviderTrace,
    RetrievalRunTrace,
)
from shared.contracts.memory import (
    MemoryEpisodeProposalSchema,
    MemoryFactSchema,
    memory_domain_to_schema,
    memory_schema_to_domain,
)
from shared.contracts.retrieval import RetrievalRunTraceSchema
from shared.contracts.retrieval import RetrievalCandidateTraceSchema
from shared.contracts.worker_messages import WorkerExecutionMessageContentSchema


def _fact() -> MemoryFact:
    return MemoryFact(
        scope_kind="session",
        scope_id="session-1",
        subject_key="session:session-1",
        fact_key="fruit_preference",
        fact_value="苹果",
        confidence=0.9,
        asserted_at="2026-10-27T00:00:00+00:00",
        source_refs=(
            MemorySourceReference(
                source_type="turn",
                source_id="message-1",
                excerpt="我喜欢苹果",
                observed_at="2026-10-27T00:00:00+00:00",
            ),
        ),
    )


def test_memory_snapshot_round_trips_through_boundary_schema() -> None:
    fact = _fact()
    snapshot = MemoryFactExtractionSnapshot(
        turn_id="run-1",
        candidates=(
            MemoryCandidateSnapshot(
                content="fruit_preference: 苹果",
                source_turn_id="run-1",
                confidence=0.9,
                session_id="session-1",
                fact=fact,
            ),
        ),
    )

    schema = memory_domain_to_schema(snapshot)

    assert memory_schema_to_domain(type(schema).model_validate_json(schema.model_dump_json())) == snapshot


def test_metadata_schema_keeps_extension_fields_outside_domain_fact() -> None:
    metadata = MemoryRecordMetadata(
        fact=_fact(),
        extra_fields={
            "session_id": "session-1",
            "source_turn_id": "run-1",
            "custom": {"reviewed": True},
        },
    )

    schema = memory_domain_to_schema(metadata)
    restored = memory_schema_to_domain(
        type(schema).model_validate_json(schema.model_dump_json())
    )

    assert restored == metadata
    assert schema.model_dump(mode="json")["custom"] == {"reviewed": True}


def test_execution_evidence_round_trips_nested_task_and_source_records() -> None:
    evidence = MemoryExecutionEvidence(
        run_id="run-1",
        source_fingerprint="a" * 64,
        total_task_count=1,
        truncated=False,
        tasks=(
            MemoryTaskOutcomeEvidence(
                task_id="task-1",
                title="查找资料",
                status="completed",
                execution_generation=1,
            ),
        ),
        source_refs=_fact().source_refs,
    )

    schema = memory_domain_to_schema(evidence)

    assert memory_schema_to_domain(type(schema).model_validate_json(schema.model_dump_json())) == evidence


def test_memory_business_invariants_are_checked_without_pydantic() -> None:
    with pytest.raises(ValueError, match="memory_fact_retraction_value_invalid"):
        MemoryFact(
            scope_kind="session",
            scope_id="session-1",
            subject_key="session:session-1",
            fact_key="fruit_preference",
            fact_value="苹果",
            confidence=0.9,
            asserted_at="2026-10-27T00:00:00+00:00",
            claim_intent="retract",
            source_refs=(
                MemorySourceReference(
                    source_type="turn",
                    source_id="message-1",
                    excerpt="忘记苹果",
                    observed_at="2026-10-27T00:00:00+00:00",
                ),
            ),
        )

    with pytest.raises(ValueError):
        MemoryFactSchema.model_validate({"scope_kind": "unknown"})


def test_episode_schema_converts_llm_output_to_domain_proposal() -> None:
    schema = MemoryEpisodeProposalSchema.model_validate(
        {
            "retain": True,
            "source_ref_ids": ("source-1",),
            "summary": {
                "title": "实现任务",
                "goal": "完成 Run 生命周期调整",
                "outcome": "completed",
                "result": "完成并验证",
            },
        }
    )

    proposal = memory_schema_to_domain(schema)

    assert isinstance(proposal, MemoryEpisodeProposal)
    assert proposal.summary == MemoryEpisodeSummary(
        title="实现任务",
        goal="完成 Run 生命周期调整",
        outcome="completed",
        result="完成并验证",
    )


def test_retrieval_trace_round_trips_with_nested_domain_types() -> None:
    trace = RetrievalRunTrace(
        raw_hit_count=1,
        providers=(
            RetrievalProviderTrace(
                provider="memory",
                status="succeeded",
                duration_ms=12.5,
                candidate_count=1,
                selected_count=1,
                channels=(
                    RetrievalChannelTrace(
                        channel="vector", status="succeeded", hit_count=1
                    ),
                ),
            ),
        ),
    )
    candidate = RetrievalCandidateTrace(
        provider="memory",
        source_type="memory",
        source_id="memory-1",
        hits=(RetrievalHitTrace(channel="vector", rank=1, score=0.8),),
    )

    schema = RetrievalRunTraceSchema.from_domain(trace)
    restored = RetrievalRunTraceSchema.model_validate_json(
        schema.model_dump_json()
    ).to_domain()

    assert restored == trace
    candidate_schema = RetrievalCandidateTraceSchema.from_domain(candidate)
    assert RetrievalCandidateTraceSchema.model_validate_json(
        candidate_schema.model_dump_json()
    ).to_domain() == candidate


def test_worker_message_schema_preserves_provider_extension_fields() -> None:
    content = WorkerExecutionMessageContent(
        content="已执行",
        tool_calls=(
            WorkerToolCallContent(
                id="call-1", name="shell", arguments={"command": "pwd"}
            ),
        ),
        tool_result=WorkerToolResultContent(
            status="succeeded",
            output="/workspace",
            extra_fields={"provider_trace_id": "trace-1"},
        ),
    )

    schema = WorkerExecutionMessageContentSchema.from_domain(content)
    restored = WorkerExecutionMessageContentSchema.model_validate_json(
        schema.model_dump_json()
    ).to_domain()

    assert restored == content
    assert schema.model_dump(mode="json")["tool_result"]["provider_trace_id"] == "trace-1"
