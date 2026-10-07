"""阶段 7 Memory worker、Approval、Tools、MCP 和集成端口测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.src.application.approval import ApprovalService
from backend.src.application.memory import MemoryWorker, MemoryWriteWorkflow
from backend.src.application.tools import MCPService, ToolService
from backend.src.domain.approval import ApprovalDecision
from backend.src.domain.memory import (
    CompletedTurn,
    MemoryCandidate,
    MemoryResolution,
    ResolutionAction,
)
from backend.src.domain.tools import (
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

    async def add(self, command):
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
    written = await workflow.process(CompletedTurn("t-1", "s-1", "remember python"))
    assert written == 1
    assert memory.writes[0].metadata["source_turn_id"] == "t-1"


class Jobs:
    def __init__(self):
        self.values = [{"turn_id": "t-1", "session_id": "s-1", "user_text": "remember"}]
        self.done = []

    async def recover(self):
        return None

    async def claim(self):
        return self.values.pop(0) if self.values else None

    async def succeed(self, turn_id):
        self.done.append(turn_id)

    async def fail(self, *args, **kwargs):
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
    def __init__(self):
        self.values = {}

    async def create(self, request):
        if request.approval_id in self.values:
            return False
        self.values[request.approval_id] = request
        return True

    async def get(self, approval_id):
        return self.values.get(approval_id)

    async def list_pending(self, session_id=None):
        return list(self.values.values())

    async def list_history(self, session_id=None, *, limit=50, offset=0):
        return list(self.values.values())

    async def resolve(self, *args, **kwargs):
        return True

    async def resolve_batch(self, *args, **kwargs):
        return True


class Events:
    def __init__(self):
        self.values = []

    async def required(self, request):
        self.values.append(request)


@pytest.mark.asyncio
async def test_approval_service_is_idempotent_and_publishes_once() -> None:
    repository, events = ApprovalRepository(), Events()
    service = ApprovalService(repository, events, id_factory=lambda: "a-1")
    kwargs = dict(session_id="s-1", run_id="r-1", tool_call_id="c-1", tool_name="shell", arguments={}, risk_level="high")
    first = await service.request(**kwargs)
    second = await service.request(**kwargs, approval_id=first.approval_id)
    assert first == second
    assert len(events.values) == 1
    assert await service.resolve(first.approval_id, run_id="r-1", task_id="t-1", decision=ApprovalDecision.APPROVED)


class ToolRepository:
    def __init__(self, config):
        self.config = config

    async def list_all(self): return [self.config]
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
