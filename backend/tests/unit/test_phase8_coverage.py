"""Focused branch tests for the final runtime boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from application.approval import ApprovalService
from application.files import IngestionService
from tests.fakes.leases import LeaseCenter, lease_grant
from application.knowledge import KnowledgeBaseService
from application.memory import MemoryService
from application.tools import MCPService, ToolQueryService, ToolService
from domain.approval import ApprovalDecision, ApprovalRequest
from domain.files import (
    AdapterInfo, Attachment, AttachmentStatus, FileChunk, FileLocator, FileMetadata, ParsedDocument, StoredBlob,
)
from domain.memory import MemoryPage, MemoryListRequest, MemorySearchRequest, MemoryWriteCommand
from domain.tools import (
    JsonSchema, MCPServerConfig, RiskLevel, ToolConfig, ToolExecutionMode,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_approval_recovery_and_memory_lifecycle_branches() -> None:
    request = ApprovalRequest("a-1", "s", "r", "c", "tool", {}, "high", created_at=NOW)

    class Repo:
        async def resolve(self, *args, **kwargs): return True
        async def list_pending(self, session_id=None): return [request]
        async def list_history(self, *args, **kwargs): return [request]

    service = ApprovalService(Repo())
    assert await service.resolve("a-1", decision=ApprovalDecision.APPROVED)
    assert (await service.pending("s"))[0].approval_id == request.approval_id
    assert (await service.history("s", limit=1, offset=0))[0].approval_id == request.approval_id

    class Memory:
        async def initialize(self): self.initialized = True
        async def search(self, value): return []
        async def list_memories(self, value): return MemoryPage((), 0, {})
        async def get(self, value): return None
        async def revisions(self, value): return []
        async def create_memory(self, value): return "m"
        async def revise(self, *args, **kwargs): return None
        async def update_validity(self, *args, **kwargs): return False
        async def delete(self, value): self.deleted = value
        async def flush_access_stats(self): return 3

    memory = MemoryService(Memory())
    await memory.initialize()
    await memory.search(MemorySearchRequest("q")); await memory.list_memories(MemoryListRequest())
    await memory.get("m"); await memory.revisions("m")
    assert await memory.create_memory(MemoryWriteCommand("x")) == "m"
    assert await memory.revise("m", "x") is None
    assert await memory.update_validity("m", "expired") is False
    await memory.delete("m")
    assert await memory.flush_access_stats() == 3
    with pytest.raises(ValueError): await memory.revise("m", " ")


@pytest.mark.asyncio
async def test_tool_and_mcp_service_branches() -> None:
    stamp = NOW
    config = ToolConfig("x", ToolExecutionMode.NATIVE, None, None, "x", JsonSchema(), RiskLevel.LOW, False, True, stamp, stamp)

    class Repo:
        async def list_tools(self): return [config]
        async def get(self, name): return config if name == "x" else None
        async def upsert(self, value): self.value = value; return value

    class Exec:
        async def execute(self, value): return value

    service = ToolService(Repo(), Exec())
    assert await service.list_tools() == [config]
    assert await service.get("x") == config
    assert await service.get("missing") is None
    updated = await service.update_governance("x", risk_level="high", require_approval=True, enabled=False)
    assert updated.risk_level is RiskLevel.HIGH and updated.enabled is False
    assert await service.update_governance("missing", enabled=False) is None
    assert await service.execute(SimpleNamespace()) is not None

    class Port:
        async def register(self, name, value): return {"name": name}
        async def unregister(self, name): self.unregistered = name
        async def list_servers(self): return [{"name": "x"}]
        async def shutdown(self): self.closed = True

    mcp = MCPService(Port())
    assert await mcp.register("x", MCPServerConfig("python", (), {}, None, "none", True)) == {"name": "x"}
    await mcp.unregister("x"); assert (await mcp.list_servers())[0]["name"] == "x"; await mcp.shutdown()

    class Usage:
        async def last_called_by_tool(self): return {"x": "now"}
        async def count_calls_since(self, value): return 2

    assert await ToolQueryService(Repo()).usage() == ({}, 0)
    assert await ToolQueryService(Repo(), Usage()).usage() == ({"x": "now"}, 2)


@pytest.mark.asyncio
async def test_ingestion_and_knowledge_error_branches() -> None:
    class Attachments:
        def __init__(self):
            self.value = Attachment("a", "doc", 1, None, "kb", None, "x.txt", "text/plain", 1, "h", "k", "a", "1", AttachmentStatus.UPLOADED, (), FileMetadata(), None, NOW, NOW)
        async def get(self, key): return self.value if key == "a" else None
        async def save(self, value): self.value = value; return value

    class Chunks:
        _leases = LeaseCenter()
        async def replace_for_attachment(self, key, values, *, lease): self.values = values

    class Parser:
        async def parse(self, value): return ParsedDocument("hello", FileMetadata(), adapter_name="parsed", adapter_version="2", capabilities=("text",))

    class Index:
        async def index_attachment(self, attachment, chunks, *, lease): self.indexed = True
        async def delete_attachment(self, key): self.deleted = key

    def chunker(text): return [FileChunk("c", "pending", 0, text, 1, FileLocator(), FileMetadata())]
    service = IngestionService(Attachments(), Chunks(), Parser(), Index(), Index(), chunker=chunker, clock=lambda: NOW, lease_center=LeaseCenter())
    result = await service.process_attachment("a", lease=lease_grant("document_ingestion", "a"))
    assert result["status"] == "ready"
    await service.delete_indexes("a")
    with pytest.raises(LookupError): await service.process_attachment("missing", lease=lease_grant("document_ingestion", "missing"))

    class Bases:
        async def get(self, key): return None
        async def list_knowledge_bases(self): return []
        async def create(self, value): return value
        async def save(self, value): return value
        async def delete(self, key): return False
    class Docs:
        async def list_documents(self, key): return []
        async def list_versions(self, *args, **kwargs): return []
        async def latest_for_filename(self, *args, **kwargs): return None
        async def create(self, value): return value
        async def soft_delete(self, *args, **kwargs): return False
    class Storage:
        async def save_stream(self, chunks): return StoredBlob("h", 1, "k")
        async def discard(self, key): pass
    class Adapters:
        def select(self, *args): return AdapterInfo("text", "1", (), (".txt",), ())
        def supported_extensions(self): return [".txt"]
    class Jobs:
        async def enqueue(self, key): pass
        async def cancel(self, key): pass
    kb = KnowledgeBaseService(Bases(), Docs(), Storage(), Adapters(), Jobs(), id_factory=lambda: "id", clock=lambda: NOW)
    with pytest.raises(Exception): await kb.get("missing")
    with pytest.raises(ValueError): await kb.create_knowledge_base(" ")
    with pytest.raises(Exception): await kb.update_knowledge_base("missing", "x")
    with pytest.raises(Exception): await kb.list_documents("missing")
    with pytest.raises(Exception): await kb.list_versions("missing", "a")
    with pytest.raises(Exception): await kb.delete_document("missing", "a")
    assert await kb.list_knowledge_bases() == [] and await kb.supported_extensions() == [".txt"]
