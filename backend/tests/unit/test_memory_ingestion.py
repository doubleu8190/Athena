"""Regression tests for the production memory-ingestion closure."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from application.memory import (
    DefaultMemoryWriteTrigger,
    LLMFactExtractor,
    LLMMemoryResolver,
    MemoryWriteWorkflow,
)
from application.runs import RunExecutionService
from domain.memory import (
    CompletedTurn,
    MemoryCandidate,
    MemoryListRequest,
    MemoryPage,
    MemoryRecord,
    MemoryWriteCommand,
)
from domain.runs import (
    CommandExecutionResult,
    CommandType,
    RunCommand,
    RunStartPayload,
    RunStatus,
)


class Memory:
    def __init__(self) -> None:
        self.items: dict[str, MemoryRecord] = {}

    async def list_memories(self, request: MemoryListRequest) -> MemoryPage:
        values = tuple(self.items.values())
        return MemoryPage(values, len(values), {"total": len(values)})

    async def create_memory(self, command: MemoryWriteCommand) -> str:
        key = command.operation_key
        if key not in self.items:
            self.items[key] = MemoryRecord(key, command.content, dict(command.metadata))
        return key

    async def revise(self, memory_id: str, content: str, **kwargs) -> str | None:
        value = self.items.get(memory_id)
        if value is None:
            return None
        self.items[memory_id] = MemoryRecord(
            memory_id, content, dict(kwargs.get("metadata") or {})
        )
        return memory_id


class LLM:
    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return {
                "content": (
                    '```json\n{"atomic_facts":['
                    '{"key":"preferred_fruit","value":"苹果",'
                    '"category":"preference","confidence":1.0},'
                    '{"key":"skill","value":"Java、Python、Golang",'
                    '"category":"profile","confidence":0.95}]}\n```'
                )
            }
        return {"content": '{"action":"CREATE"}'}


class Vectors:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    async def index(self, memory_id: str, content: str) -> None:
        self.items.append((memory_id, content))
async def test_explicit_preferences_are_extracted_and_indexed() -> None:
    memory, vectors = Memory(), Vectors()
    llm = LLM()
    workflow = MemoryWriteWorkflow(
        memory,
        DefaultMemoryWriteTrigger(),
        LLMFactExtractor(llm, memory),
        LLMMemoryResolver(llm, memory),
        vectors,
    )
    written = await workflow.process_completed_turn(
        CompletedTurn(
            "turn-1",
            "session-1",
            "我喜欢苹果，也擅长 Java、Python、Golang",
            "好的",
        )
    )
    assert written == 2
    assert {item.content for item in memory.items.values()} == {
        "preferred_fruit: 苹果",
        "skill: Java、Python、Golang",
    }
    assert len(vectors.items) == 2


@pytest.mark.asyncio
async def test_resolution_ignores_an_exact_duplicate() -> None:
    memory, vectors = Memory(), Vectors()
    existing = await memory.create_memory(
        MemoryWriteCommand(
            "preferred_fruit: 苹果",
            {"session_id": "session-1"},
            operation_key="turn:old:0",
        )
    )
    resolutions = await LLMMemoryResolver(LLM(), memory).resolve(
        [
            MemoryCandidate(
                "preferred_fruit: 苹果",
                "turn-2",
                "preference",
                "preference",
                1.0,
                "session-1",
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

    async def update_status(self, *args):
        return True

    async def complete(self, *args):
        return True

    async def fail(self, *args):
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
