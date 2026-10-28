"""Regression tests for the production memory-ingestion closure."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from application.memory import (
    LLMFactExtractor,
    MemoryFactResolver,
    MemoryWriteWorkflow,
)
from application.runs import RunExecutionService
from domain.memory import (
    CompletedTurn,
    FactCardinality,
    MemoryCandidate,
    MemoryFact,
    MemoryListRequest,
    MemoryPage,
    MemoryRecord,
    MemorySourceReference,
    MemoryWriteCommand,
)
from domain.runs import (
    CommandExecutionResult,
    CommandType,
    RunCommand,
    RunStartPayload,
    RunStatus,
)
from tests.fakes.leases import lease_grant


from tests.fakes.memory_pipeline import Memory, Embedder, Snapshots, dependencies


class LLM:
    def __init__(self) -> None:
        self.calls = 0

    async def invoke_structured(self, messages, schema):
        self.calls += 1
        import json
        return schema.model_validate_json(json.dumps({"atomic_facts": [
            {"key": "favorite_fruit", "value": "苹果", "category": "preference", "scope_kind": "global", "cardinality": "single", "confidence": 1.0},
            {"key": "skills", "value": "Java、Python、Golang", "category": "profile", "scope_kind": "global", "cardinality": "set", "confidence": 0.95},
        ]}))


async def test_explicit_preferences_are_extracted_and_indexed() -> None:
    memory, vectors = Memory(), Embedder()
    llm = LLM()
    workflow = MemoryWriteWorkflow(
        memory,
        LLMFactExtractor(llm, snapshots=Snapshots()),
        MemoryFactResolver(memory),
        vectors,
        **dependencies(),
    )
    written = await workflow.process_completed_turn(
        CompletedTurn(
            "turn-1",
            "session-1",
            "我喜欢苹果，也擅长 Java、Python、Golang",
            "好的",
            project_id="project-1",
        ),
        lease=lease_grant("memory_processing", "turn-1"),
    )
    assert written == 2
    assert {item.content for item in memory.items.values()} == {
        "favorite_fruit: 苹果",
        "skills: Java、Python、Golang",
    }
    cardinalities = {
        item.metadata["fact"]["fact_key"]: item.metadata["fact"]["cardinality"]
        for item in memory.items.values()
    }
    assert cardinalities == {"favorite_fruit": "single", "skills": "set"}
    assert len(vectors.items) == 2


@pytest.mark.asyncio
async def test_resolution_ignores_an_exact_duplicate() -> None:
    memory, vectors = Memory(), Embedder()
    fact = MemoryFact(
        scope_kind="global",
        scope_id="global",
        subject_key="user:self",
        fact_key="preferred_fruit",
        fact_value="苹果",
        cardinality=FactCardinality.SINGLE,
        confidence=1.0,
        asserted_at=datetime.now(timezone.utc).isoformat(),
        source_refs=(
            MemorySourceReference(
                source_type="turn",
                source_id="turn-1",
                excerpt="我喜欢苹果。",
                observed_at=datetime.now(timezone.utc).isoformat(),
            ),
        ),
    )
    existing = await memory.create_memory(
        MemoryWriteCommand(
            "preferred_fruit: 苹果",
            {"session_id": "session-1", "fact": fact},
            operation_key="turn:old:0",
        )
    )
    resolutions = await MemoryFactResolver(memory).resolve(
        [
            MemoryCandidate(
                "preferred_fruit: 苹果",
                "turn-2",
                "preference",
                "preference",
                1.0,
                "session-1",
                fact=fact,
            )
        ]
    )
    assert resolutions[0].target_memory_id == existing
    assert resolutions[0].action.value == "ignore"


class Jobs:
    def __init__(self) -> None:
        self.items = []

    async def enqueue(self, **values):
        self.items.append(values)
        return True


class Lifecycle:
    writes_cancellation_events_to_outbox = False
    writes_terminal_artifacts_to_outbox = False

    async def update_status(self, *args, **kwargs):
        return True

    async def complete(self, *args, **kwargs):
        return True

    async def fail(self, *args, **kwargs):
        return True

    async def arm_root_wait(self, *args, **kwargs):
        return True


class Events:
    async def publish(self, event):
        return event


class Executor:
    async def execute(self, command):
        return CommandExecutionResult(
            result={
                "content": "苹果很受欢迎",
                "answer_stream": {
                    "message_id": "assistant:run-1", "stream_id": "answer-test",
                    "started_at": datetime.now(timezone.utc).isoformat(), "last_chunk_id": 1,
                },
            },
        )

    async def cancel(self, _run_id):
        return None


@pytest.mark.asyncio
async def test_completed_run_enqueues_memory_without_blocking_answer() -> None:
    jobs = Jobs()
    service = RunExecutionService(
        Lifecycle(), Executor(), Events(), memory_jobs=jobs
    )
    command = RunCommand(
        "command-1",
        CommandType.RUN_START,
        "session-1",
        "run-1",
        RunStartPayload(message="我喜欢苹果", message_id="message-1"),
        datetime.now(timezone.utc),
        lease=lease_grant("root_execution", "run-1"),
    )
    await service.execute(command)
    assert jobs.items == [
        {
            "turn_id": "run-1",
            "session_id": "session-1",
            "user_text": "我喜欢苹果",
            "assistant_text": "苹果很受欢迎",
        }
    ]
