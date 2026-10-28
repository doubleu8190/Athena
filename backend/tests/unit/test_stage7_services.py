"""阶段 7 Memory worker、Approval、Tools、MCP 和集成端口测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from application.approval import ApprovalService
from application.memory import MemoryWriteWorkflow
from workers import MemoryWorker
from application.tools import MCPService, ToolService
from domain.approval import ApprovalDecision
from domain.leases import LeaseLostError
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
from tests.fakes.memory_pipeline import Memory, Embedder, dependencies, candidate
from tests.fakes.leases import LeaseCenter, lease_grant


class Extractor:
    async def extract(self, turn, *, lease):
        return [candidate(turn.turn_id)]


class Resolver:
    async def resolve(self, candidates):
        return [MemoryResolution(ResolutionAction.CREATE, candidates[0])]


@pytest.mark.asyncio
async def test_memory_workflow_writes_resolved_candidates() -> None:
    memory = Memory()
    workflow = MemoryWriteWorkflow(memory, Extractor(), Resolver(), Embedder(), **dependencies())
    written = await workflow.process_completed_turn(
        CompletedTurn("t-1", "s-1", "remember python", project_id="project-1"),
        lease=lease_grant("memory_processing", "t-1"),
    )
    assert written == 1
    assert next(iter(memory.items.values())).metadata["source_turn_id"] == "t-1"


class Jobs:
    def __init__(self):
        self._leases = LeaseCenter()
        self.values = [ClaimedMemoryJob(
            CompletedTurn("t-1", "s-1", "remember", project_id="project-1"), 1,
            lease_grant("memory_processing", "t-1"),
        )]
        self.done = []

    async def claim_next_job(self, *, lease_seconds):
        return self.values.pop(0) if self.values else None

    async def mark_succeeded(self, turn_id, *, lease):
        self.done.append(turn_id)

    async def mark_failed(self, *args, **kwargs):
        raise AssertionError("job should succeed")


@pytest.mark.asyncio
async def test_memory_worker_recovers_and_stops() -> None:
    jobs = Jobs()
    worker = MemoryWorker(
        jobs, MemoryWriteWorkflow(Memory(), Extractor(), Resolver(), Embedder(), **dependencies()),
        poll_interval=0.001, lease_center=jobs._leases,
    )
    task = __import__("asyncio").create_task(worker.run())
    while not jobs.done:
        await __import__("asyncio").sleep(0.001)
    worker.stop()
    await task
    assert jobs.done == ["t-1"]


@pytest.mark.asyncio
async def test_memory_worker_acknowledges_lost_lease_and_continues() -> None:
    class LeaseCenterWithAcknowledgements(LeaseCenter):
        def __init__(self):
            super().__init__()
            self.acknowledged = []

        async def acknowledge_stopped(self, grant, *, completed=False):
            self.acknowledged.append(grant)
            return True

    class TwoJobs(Jobs):
        def __init__(self, leases):
            self._leases = leases
            self.values = [
                ClaimedMemoryJob(
                    CompletedTurn("t-lost", "s-1", "first", project_id="project-1"),
                    1,
                    lease_grant("memory_processing", "t-lost"),
                ),
                ClaimedMemoryJob(
                    CompletedTurn("t-next", "s-1", "next", project_id="project-1"),
                    1,
                    lease_grant("memory_processing", "t-next"),
                ),
            ]
            self.done = []

    class Workflow:
        async def process_completed_turn(self, turn, *, lease):
            if turn.turn_id == "t-lost":
                raise LeaseLostError(lease.key)

    leases = LeaseCenterWithAcknowledgements()
    jobs = TwoJobs(leases)
    worker = MemoryWorker(
        jobs, Workflow(), poll_interval=0.001, lease_center=leases
    )
    task = asyncio.create_task(worker.run())
    for _ in range(1000):
        if jobs.done:
            break
        await asyncio.sleep(0.001)
    else:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        pytest.fail("worker did not process the job after losing the first lease")
    worker.stop()
    await task

    assert len(leases.acknowledged) == 1
    assert leases.acknowledged[0].key.resource_id == "t-lost"
    assert jobs.done == ["t-next"]


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
