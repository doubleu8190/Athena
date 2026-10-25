"""阶段 7 Memory worker、Approval、Tools、MCP 和集成端口测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from application.approval import ApprovalService
from application.memory import MemoryWriteWorkflow
from workers import MemoryWorker
from application.tools import MCPService, ToolService
from domain.approval import ApprovalDecision
from domain.memory import (
    ClaimedMemoryJob,
    CompletedTurn,
    MemoryCandidate,
    MemoryResolution,
    ResolutionAction,
)
from domain.tools import (
    JsonSchema,
    MCPServerConfig,
    RiskLevel,
    ToolConfig,
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolExecutionMode,
)


class MemoryPort:
    def __init__(self):
        self.writes = []

    async def create_memory(self, command):
        self.writes.append(command)
        return "m-1"

    async def revise(self, *args, **kwargs):
        return "m-2"


class Trigger:
    def should_process(self, turn):
        return True


class Extractor:
    async def extract(self, turn):
        return [MemoryCandidate("pref: python", turn.turn_id)]


class Resolver:
    async def resolve(self, candidates):
        return [MemoryResolution(ResolutionAction.CREATE, candidates[0])]


@pytest.mark.asyncio
async def test_memory_workflow_writes_resolved_candidates() -> None:
    memory = MemoryPort()
    workflow = MemoryWriteWorkflow(memory, Trigger(), Extractor(), Resolver())
    written = await workflow.process_completed_turn(CompletedTurn("t-1", "s-1", "remember python"))
    assert written == 1
    assert memory.writes[0].metadata["source_turn_id"] == "t-1"


class Jobs:
    def __init__(self):
        self.values = [ClaimedMemoryJob(CompletedTurn("t-1", "s-1", "remember"), 1)]
        self.done = []

    async def recover(self):
        return None

    async def claim_next_job(self):
        return self.values.pop(0) if self.values else None

    async def mark_succeeded(self, turn_id):
        self.done.append(turn_id)

    async def mark_failed(self, *args, **kwargs):
        raise AssertionError("job should succeed")


@pytest.mark.asyncio
async def test_memory_worker_recovers_and_stops() -> None:
    jobs = Jobs()
    worker = MemoryWorker(jobs, MemoryWriteWorkflow(MemoryPort(), Trigger(), Extractor(), Resolver()), poll_interval=0.001)
    task = __import__("asyncio").create_task(worker.run())
    while not jobs.done:
        await __import__("asyncio").sleep(0.001)
    worker.stop()
    await task
    assert jobs.done == ["t-1"]


class ApprovalRepository:
    async def resolve(self, approval_id, *, decision):
        self.resolved = (approval_id, decision)
        return True


@pytest.mark.asyncio
async def test_approval_service_resolves_by_authoritative_approval_id() -> None:
    repository = ApprovalRepository()
    service = ApprovalService(repository)
    assert await service.resolve("a-1", decision=ApprovalDecision.APPROVED)
    assert repository.resolved == ("a-1", ApprovalDecision.APPROVED)


class ToolRepository:
    def __init__(self, config):
        self.config = config

    async def list_tools(self): return [self.config]
    async def get(self, name): return self.config if name == self.config.tool_name else None
    async def upsert(self, config): self.config = config; return config


class Execution:
    async def execute(self, request):
        return ToolExecutionResult("success", output=request.tool_name)


@pytest.mark.asyncio
async def test_tool_service_updates_governance_and_executes() -> None:
    config = ToolConfig("shell", ToolExecutionMode.NATIVE, None, None, "shell", JsonSchema(), RiskLevel.MEDIUM, True, True, datetime.now(timezone.utc), datetime.now(timezone.utc))
    service = ToolService(ToolRepository(config), Execution())
    updated = await service.update_governance("shell", enabled=False)
    assert updated is not None and not updated.enabled
    result = await service.execute(ToolExecutionRequest("shell", {}, "s-1", "r-1", "c-1"))
    assert result.status == "success"


@pytest.mark.asyncio
async def test_mcp_service_delegates_port() -> None:
    class Port:
        async def register(self, name, config): return {"name": name}
        async def unregister(self, name): return None
        async def list_servers(self): return [{"name": "x"}]
        async def shutdown(self): return None

    service = MCPService(Port())
    result = await service.register("x", MCPServerConfig("python", (), {}, None, "none", True))
    assert result["name"] == "x"
