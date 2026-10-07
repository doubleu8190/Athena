from datetime import datetime, timezone

import pytest

from backend.src.domain.approval import ApprovalDecision, ApprovalRequest
from backend.src.domain.events import ApplicationEvent, EventDurability, EventType
from backend.src.domain.files import Attachment, AttachmentStatus, FileMetadata, KnowledgeBase
from backend.src.domain.memory import MemoryListRequest, MemorySearchRequest, MemoryWriteCommand
from backend.src.domain.runs import CommandType, RunCommand, RunStatus
from backend.src.domain.tools import MCPServerConfig, ToolExecutionRequest
from backend.src.infrastructure.in_memory import *

NOW = datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_default_session_message_run_and_tool_adapters():
    sessions, messages, runs = InMemorySessionRepository(), InMemoryMessageRepository(), InMemoryRunStore()
    session = __import__("backend.src.domain.sessions", fromlist=["Session"]).Session.create(session_id="s", title="x", now=NOW)
    await sessions.create(session); assert await sessions.get("s") == session
    assert await sessions.list_all() == [session]; await sessions.save(session.rename("y", now=NOW)); assert await sessions.delete("missing") is False
    command = RunCommand("c", CommandType.RUN_START, "s", "r", {"message_id": "m"}, NOW)
    result = await runs.enqueue_command(command); assert result.status == "queued"
    assert (await runs.claim_next_command()).command_id == "c"; await runs.complete_command("c", status="completed")
    assert await runs.get_command("c")
    await runs.update_status("r", RunStatus.RUNNING); assert await runs.get_active_for_session("s")
    assert await runs.complete("missing", {}) is False
    tool_repo = InMemoryToolRepository(); assert await tool_repo.get("noop")
    tool = InMemoryToolExecution(); assert (await tool.execute(ToolExecutionRequest("noop", {}, "s", "r", "t"))).status == "success"
    message = __import__("backend.src.domain.sessions", fromlist=["Message", "MessageRole"]).Message("m", "s", __import__("backend.src.domain.sessions", fromlist=["MessageRole"]).MessageRole.USER, "hi", NOW)
    await messages.save(message); assert await messages.list_by_session("s") == [message]
    assert await messages.get("m") == message
    assert await messages.list_after("s", "a") == [message]
    assert await runs.get("r") is not None; assert await runs.list_for_session("s")
    assert await runs.fail("r", {}) and await runs.cancel("r")
    assert await runs.claim_next_command() is None
    assert (await runs.enqueue_command(command)).deduplicated is True
    await tool_repo.upsert((await tool_repo.get("noop"))); await tool_repo.update((await tool_repo.get("noop"))); assert await tool_repo.list_all()


@pytest.mark.asyncio
async def test_memory_approval_mcp_and_event_adapters():
    memory = InMemoryMemoryPort(); key = await memory.add(MemoryWriteCommand("hello", operation_key="m"))
    assert (await memory.search(MemorySearchRequest("hel")))[0].id == key
    assert (await memory.list(MemoryListRequest())).total == 1; assert await memory.revise(key, "updated") == key
    assert await memory.set_validity(key, "valid"); await memory.delete(key); assert (await memory.get(key)).status.value == "deleted"
    approvals = InMemoryApprovalRepository(); request = ApprovalRequest("a", "s", "r", "t", "noop", {}, "low", created_at=NOW)
    assert await approvals.create(request); assert len(await approvals.list_pending()) == 1
    assert await approvals.resolve("a", run_id="r", task_id="", decision=ApprovalDecision.APPROVED)
    assert await approvals.list_history(); assert not await approvals.resolve("missing", run_id="r", task_id="", decision=ApprovalDecision.DENIED)
    assert await approvals.resolve_batch("b", {}) is False
    mcp = InMemoryMCPPort(); config = MCPServerConfig("echo", (), {}, None, "none", True)
    assert (await mcp.register("x", config))["status"] == "connected"; assert len(await mcp.list_servers()) == 1; await mcp.unregister("x"); await mcp.shutdown()
    events = InMemoryEventStore(); event = ApplicationEvent(EventType.RUN_STARTED, EventDurability.DURABLE, "s")
    await events.publish(event); queue, watermark = await events.open_subscription("s"); assert watermark == 1
    await events.publish(ApplicationEvent(EventType.RUN_COMPLETED, EventDurability.DURABLE, "s")); assert (await queue.get()).session_seq == 2
    assert len(await events.list_events_between("s", 0, 2)) == 2; await events.close_subscription("s", queue)
    await InMemoryEventPublisher(events).publish_realtime(event)


@pytest.mark.asyncio
async def test_default_file_and_knowledge_adapters():
    storage, adapters = InMemoryFileStorage(), InMemoryFileAdapters()
    blob = await storage.save_stream(_chunks([b"hello"])); assert blob.size_bytes == 5
    assert adapters.select("a.txt", "text/plain").name == "text"; assert ".txt" in adapters.supported_extensions()
    jobs = InMemoryDocumentJobs(); await jobs.enqueue("a"); await jobs.cancel("a")
    attachments = InMemoryAttachmentRepository(); value = Attachment("a", "d", 1, "s", None, None, "a.txt", "text/plain", 5, blob.sha256, blob.storage_key, "text", "1", AttachmentStatus.UPLOADED, ("text",), FileMetadata(), None, NOW, NOW)
    await attachments.create(value); assert await attachments.get("a", session_id="s") == value; assert await attachments.soft_delete("a", session_id="s")
    assert await attachments.list_by_session("s") == []; assert await attachments.list_by_knowledge_base("missing") == []; await attachments.save(value)
    assert await attachments.get("a", session_id="s") is not None; await storage.cleanup_unreferenced(set()); await storage.discard(blob.storage_key)
    bases = InMemoryKnowledgeRepository(); kb = KnowledgeBase("k", "K", "", 0, 0, 0, NOW, NOW); await bases.create(kb); assert await bases.list_all() == [kb]; await bases.delete("k")


async def _chunks(values):
    for value in values: yield value
