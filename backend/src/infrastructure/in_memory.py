"""Small in-process adapters used by the bootstrap smoke application.

These adapters implement the same domain ports as the PostgreSQL adapters.  They
make the default ASGI app deterministic for local development and tests; a
production deployment can replace the providers with the PostgreSQL graph
without changing controllers or application services.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from domain.approval import ApprovalDecision, ApprovalRequest
from domain.common.query_ports import (
    MemoryQueryPort,
    ProviderQueryPort,
    RetrievalQueryPort,
    SettingsQueryPort,
    ToolUsageQueryPort,
)
from domain.files import (
    AdapterInfo,
    Attachment,
    AttachmentRepository,
    AttachmentStatus,
    DocumentJobPort,
    FileAdapterRegistryPort,
    FileStoragePort,
    KnowledgeBase,
    KnowledgeDocumentRepository,
    StoredBlob,
)
from domain.events import ApplicationEvent
import asyncio
import hashlib
from domain.memory import (
    MemoryListRequest,
    MemoryPage,
    MemoryRecord,
    MemorySearchRequest,
    MemoryStatus,
    MemoryWriteCommand,
)
from domain.runs import (
    CommandEnqueueResult,
    CommandStatus,
    CommandStatusRecord,
    RunCommand,
    RunQueryPort,
    RunStatus,
    RunSummary,
)
from domain.sessions import Message, Session
from domain.tools import (
    JsonSchema,
    MCPServerConfig,
    RiskLevel,
    ToolConfig,
    ToolExecutionPort,
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolExecutionMode,
)


class InMemorySessionRepository:
    def __init__(self) -> None:
        self.items: dict[str, Session] = {}

    async def create(self, session: Session) -> Session:
        self.items[session.id] = session
        return session

    async def get(self, session_id: str) -> Session | None:
        return self.items.get(session_id)

    async def list_all(self) -> list[Session]:
        return sorted(self.items.values(), key=lambda item: item.updated_at, reverse=True)

    async def save(self, session: Session) -> Session:
        self.items[session.id] = session
        return session

    async def delete(self, session_id: str) -> bool:
        return self.items.pop(session_id, None) is not None


class InMemoryMessageRepository:
    def __init__(self) -> None:
        self.items: dict[str, Message] = {}

    async def save(self, message: Message) -> str:
        self.items[message.id] = message
        return message.id

    async def get(self, message_id: str) -> Message | None:
        return self.items.get(message_id)

    async def list_by_session(self, session_id: str, *, limit: int | None = None) -> list[Message]:
        values = [item for item in self.items.values() if item.session_id == session_id]
        values.sort(key=lambda item: item.timestamp)
        return values if limit is None else values[:limit]

    async def list_after(self, session_id: str, after_id: str) -> list[Message]:
        values = await self.list_by_session(session_id)
        return [item for item in values if item.id > after_id]


class InMemoryRunStore(RunQueryPort):
    def __init__(self) -> None:
        self.commands: dict[str, RunCommand] = {}
        self.command_status: dict[str, CommandStatusRecord] = {}
        self.runs: dict[str, RunSummary] = {}

    async def enqueue_command(self, command: RunCommand) -> CommandEnqueueResult:
        existing = self.command_status.get(command.command_id)
        if existing is not None:
            return CommandEnqueueResult(command.command_id, command.run_id, command.payload.get("message_id"), tuple(command.payload.get("attachment_ids", ())), existing.status, True)
        self.commands[command.command_id] = command
        self.command_status[command.command_id] = CommandStatusRecord(command.command_id, command.session_id, command.run_id, command.command_type, CommandStatus.QUEUED)
        if command.run_id:
            now = datetime.now(timezone.utc)
            self.runs.setdefault(command.run_id, RunSummary(command.run_id, command.session_id, RunStatus.CREATED, f"thread-{command.run_id}", created_at=now, updated_at=now))
        return CommandEnqueueResult(command.command_id, command.run_id, command.payload.get("message_id"), tuple(command.payload.get("attachment_ids", ())), CommandStatus.QUEUED, False)

    async def get_command(self, command_id: str) -> CommandStatusRecord | None:
        return self.command_status.get(command_id)

    async def list_for_session(self, session_id: str) -> list[RunSummary]:
        return [value for value in self.runs.values() if value.session_id == session_id]

    async def get(self, run_id: str) -> RunSummary | None:
        return self.runs.get(run_id)

    async def get_active_for_session(self, session_id: str) -> RunSummary | None:
        return next((value for value in self.runs.values() if value.session_id == session_id and value.status in {RunStatus.CREATED, RunStatus.RUNNING, RunStatus.PAUSED}), None)

    async def claim_next_command(self) -> RunCommand | None:
        for command_id, command in self.commands.items():
            status = self.command_status[command_id]
            if status.status == CommandStatus.QUEUED:
                self.command_status[command_id] = replace(status, status=CommandStatus.RUNNING)
                return command
        return None

    async def complete_command(self, command_id: str, *, status: CommandStatus, result: object = None, error: object = None) -> None:
        current = self.command_status.get(command_id)
        if current:
            self.command_status[command_id] = replace(current, status=status, result=result, error=error)

    async def update_status(self, run_id: str, status: RunStatus | str) -> bool:
        current = self.runs.get(run_id)
        if current is None:
            return False
        self.runs[run_id] = replace(current, status=status, updated_at=datetime.now(timezone.utc))
        return True

    async def complete(self, run_id: str, result: dict[str, object]) -> bool:
        return await self.update_status(run_id, RunStatus.COMPLETED)

    async def fail(self, run_id: str, error: dict[str, object]) -> bool:
        return await self.update_status(run_id, RunStatus.FAILED)

    async def cancel(self, run_id: str) -> bool:
        return await self.update_status(run_id, RunStatus.CANCELLED)


class InMemoryToolRepository:
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.items: dict[str, ToolConfig] = {
            "noop": ToolConfig("noop", ToolExecutionMode.NATIVE, None, None, "No-op tool", JsonSchema({}), RiskLevel.LOW, False, True, now, now)
        }

    async def upsert(self, config: ToolConfig) -> ToolConfig:
        self.items[config.tool_name] = config
        return config

    async def update(self, config: ToolConfig) -> ToolConfig:
        return await self.upsert(config)

    async def get(self, tool_name: str) -> ToolConfig | None:
        return self.items.get(tool_name)

    async def list_all(self) -> list[ToolConfig]:
        return list(self.items.values())


class InMemoryToolExecution(ToolExecutionPort):
    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        return ToolExecutionResult(status="success", output="", duration_ms=0)


class InMemoryMCPPort:
    def __init__(self) -> None:
        self.items: dict[str, MCPServerConfig] = {}

    async def register(self, name: str, config: MCPServerConfig) -> dict[str, object]:
        self.items[name] = config
        return {"name": name, "status": "connected"}

    async def unregister(self, name: str) -> None:
        self.items.pop(name, None)

    async def list_servers(self) -> list[dict[str, object]]:
        return [{"name": name, "status": "connected", "enabled": config.enabled} for name, config in self.items.items()]

    async def shutdown(self) -> None:
        self.items.clear()


class InMemoryMemoryPort:
    def __init__(self) -> None:
        self.items: dict[str, MemoryRecord] = {}

    async def initialize(self) -> None: pass
    async def search(self, request: MemorySearchRequest) -> list[MemoryRecord]:
        query = request.query.lower()
        return [replace(value, score=1.0) for value in self.items.values() if query in value.content.lower()][:request.limit]
    async def list(self, request: MemoryListRequest) -> MemoryPage:
        values = list(self.items.values())[request.offset:request.offset + request.limit]
        return MemoryPage(tuple(values), len(self.items), {"total": len(self.items), "expired": 0, "recent": len(self.items)})
    async def get(self, memory_id: str) -> MemoryRecord | None: return self.items.get(memory_id)
    async def revisions(self, memory_id: str) -> list[MemoryRecord]:
        value = self.items.get(memory_id)
        return [] if value is None else [value]
    async def add(self, command: MemoryWriteCommand) -> str:
        key = command.operation_key or f"memory-{len(self.items) + 1}"
        self.items[key] = MemoryRecord(key, command.content, dict(command.metadata))
        return key
    async def revise(self, memory_id: str, content: str, *, metadata: dict[str, object] | None = None, operation_key: str | None = None) -> str | None:
        if memory_id not in self.items: return None
        self.items[memory_id] = replace(self.items[memory_id], content=content, metadata=metadata or self.items[memory_id].metadata)
        return memory_id
    async def set_validity(self, memory_id: str, validity_status: str, *, valid_until: str | None = None) -> bool: return memory_id in self.items
    async def delete(self, memory_id: str) -> None:
        if memory_id in self.items: self.items[memory_id] = replace(self.items[memory_id], status=MemoryStatus.DELETED)
    async def flush_access_stats(self) -> int: return 0


class InMemoryApprovalRepository:
    def __init__(self) -> None: self.items: dict[str, ApprovalRequest] = {}
    async def create(self, request: ApprovalRequest) -> bool:
        if request.approval_id in self.items: return False
        self.items[request.approval_id] = request
        return True
    async def get(self, approval_id: str) -> ApprovalRequest | None: return self.items.get(approval_id)
    async def list_pending(self, session_id: str | None = None) -> list[ApprovalRequest]:
        return [value for value in self.items.values() if value.decision is None and (session_id is None or value.session_id == session_id)]
    async def list_history(self, session_id: str | None = None, *, limit: int = 50, offset: int = 0) -> list[ApprovalRequest]:
        values = [value for value in self.items.values() if value.decision is not None and (session_id is None or value.session_id == session_id)]
        return values[offset:offset + limit]
    async def resolve(self, approval_id: str, *, run_id: str, task_id: str, decision: ApprovalDecision) -> bool:
        value = self.items.get(approval_id)
        if value is None or value.decision is not None or value.run_id != run_id: return False
        self.items[approval_id] = replace(value, decision=decision, status=decision.value)
        return True
    async def resolve_batch(self, batch_id: str, decisions: dict[str, ApprovalDecision], *, run_id: str | None = None) -> bool:
        changed = False
        for key, decision in decisions.items(): changed = await self.resolve(key, run_id=run_id or self.items[key].run_id, task_id=self.items[key].task_id or "", decision=decision) or changed
        return changed


class InMemoryApprovalEvents:
    async def required(self, request: ApprovalRequest) -> None: pass


class InMemoryProviderQueries(ProviderQueryPort, SettingsQueryPort, RetrievalQueryPort, ToolUsageQueryPort, MemoryQueryPort):
    def __init__(self, settings: dict[str, Any], memory: InMemoryMemoryPort) -> None:
        self.settings = settings
        self.memory = memory
    async def list_providers(self) -> list[dict[str, Any]]: return self.settings.get("providers", [])
    async def get_settings(self) -> dict[str, Any]: return self.settings
    async def last_called_by_tool(self) -> dict[str, str]: return {}
    async def count_calls_since(self, since: Any) -> int: return 0
    @staticmethod
    def _record(value: MemoryRecord) -> dict[str, Any]:
        return {"id": value.id, "content": value.content, "metadata": dict(value.metadata), "score": value.score, "source": value.source, "status": value.status.value}
    async def search(self, *, query: str, n_results: int, where: dict[str, Any] | None) -> list[dict[str, Any]]: return [self._record(value) for value in await self.memory.search(MemorySearchRequest(query=query, limit=n_results))]
    async def list(self, *, limit: int, offset: int, expired: bool, session_id: str | None) -> dict[str, Any]:
        page = await self.memory.list(MemoryListRequest(limit=limit, offset=offset, expired_only=expired, session_id=session_id)); return {"items": [self._record(value) for value in page.items], **page.stats}
    async def get(self, memory_id: str) -> dict[str, Any] | None:
        value = await self.memory.get(memory_id); return None if value is None else self._record(value)
    async def revisions(self, memory_id: str) -> list[dict[str, Any]]: return [self._record(value) for value in await self.memory.revisions(memory_id)]
    async def list_runs(self, **filters: Any) -> tuple[list[dict[str, Any]], int]: return ([], 0)
    async def get_run(self, run_id: str) -> dict[str, Any] | None: return None


class InMemoryKnowledgeRepository:
    def __init__(self) -> None: self.items: dict[str, KnowledgeBase] = {}
    async def create(self, item: KnowledgeBase) -> KnowledgeBase: self.items[item.id] = item; return item
    async def get(self, item_id: str) -> KnowledgeBase | None: return self.items.get(item_id)
    async def list_all(self) -> list[KnowledgeBase]: return list(self.items.values())
    async def save(self, item: KnowledgeBase) -> KnowledgeBase: self.items[item.id] = item; return item
    async def delete(self, item_id: str) -> bool: return self.items.pop(item_id, None) is not None


class InMemoryFileStorage(FileStoragePort):
    def __init__(self) -> None: self.blobs: dict[str, bytes] = {}
    async def save_stream(self, chunks):
        data = bytearray()
        async for chunk in chunks: data.extend(chunk)
        digest = hashlib.sha256(data).hexdigest(); key = digest
        self.blobs[key] = bytes(data)
        return StoredBlob(digest, len(data), key)
    async def cleanup_unreferenced(self, live_storage_keys: set[str]) -> int:
        removed = len([key for key in self.blobs if key not in live_storage_keys])
        for key in list(self.blobs):
            if key not in live_storage_keys: self.blobs.pop(key)
        return removed
    async def discard(self, storage_key: str) -> None: self.blobs.pop(storage_key, None)


class InMemoryFileAdapters(FileAdapterRegistryPort):
    def __init__(self) -> None:
        self.adapter = AdapterInfo("text", "1", ("text/plain", "application/octet-stream"), (".txt", ".md", ".json", ".csv"), ("text",))
    def select(self, filename: str, mime_type: str) -> AdapterInfo:
        if "." not in filename: raise ValueError("unsupported file type")
        return self.adapter
    def supported_extensions(self) -> list[str]: return list(self.adapter.extensions)


class InMemoryDocumentJobs(DocumentJobPort):
    def __init__(self) -> None: self.items: set[str] = set()
    async def enqueue(self, attachment_id: str) -> bool: self.items.add(attachment_id); return True
    async def cancel(self, attachment_id: str) -> None: self.items.discard(attachment_id)


class InMemoryAttachmentRepository(AttachmentRepository):
    def __init__(self) -> None: self.items: dict[str, Attachment] = {}
    async def create(self, attachment: Attachment) -> Attachment: self.items[attachment.id] = attachment; return attachment
    async def get(self, attachment_id: str, *, session_id: str | None = None) -> Attachment | None:
        value = self.items.get(attachment_id)
        return value if value and value.deleted_time is None and (session_id is None or value.session_id == session_id) else None
    async def list_by_session(self, session_id: str) -> list[Attachment]: return [v for v in self.items.values() if v.session_id == session_id and v.deleted_time is None]
    async def list_by_knowledge_base(self, knowledge_base_id: str) -> list[Attachment]: return [v for v in self.items.values() if v.knowledge_base_id == knowledge_base_id and v.deleted_time is None]
    async def save(self, attachment: Attachment) -> Attachment: self.items[attachment.id] = attachment; return attachment
    async def soft_delete(self, attachment_id: str, *, session_id: str) -> bool:
        value = await self.get(attachment_id, session_id=session_id)
        if value is None: return False
        self.items[attachment_id] = replace(value, status=AttachmentStatus.DELETED, deleted_time=datetime.now(timezone.utc)); return True


class InMemoryKnowledgeDocuments(InMemoryAttachmentRepository, KnowledgeDocumentRepository):
    async def get(self, attachment_id: str, *, knowledge_base_id: str):
        value = self.items.get(attachment_id)
        return value if value and value.knowledge_base_id == knowledge_base_id and value.deleted_time is None else None
    async def list(self, knowledge_base_id: str): return await self.list_by_knowledge_base(knowledge_base_id)
    async def list_versions(self, attachment_id: str, *, knowledge_base_id: str):
        value = self.items.get(attachment_id)
        return [] if value is None else [v for v in self.items.values() if v.logical_document_id == value.logical_document_id and v.knowledge_base_id == knowledge_base_id]
    async def latest_for_filename(self, filename: str, *, knowledge_base_id: str):
        values = [v for v in self.items.values() if v.filename == filename and v.knowledge_base_id == knowledge_base_id and v.deleted_time is None]
        return max(values, key=lambda v: v.document_version, default=None)
    async def soft_delete(self, attachment_id: str, *, knowledge_base_id: str) -> bool:
        value = await self.get(attachment_id, knowledge_base_id=knowledge_base_id)
        if value is None: return False
        self.items[attachment_id] = replace(value, status=AttachmentStatus.DELETED, deleted_time=datetime.now(timezone.utc)); return True


class InMemoryEventStore:
    def __init__(self) -> None: self.items: dict[str, list[ApplicationEvent]] = {}; self.subscribers: dict[str, list[asyncio.Queue[ApplicationEvent]]] = {}
    async def publish(self, event: ApplicationEvent) -> ApplicationEvent:
        values = self.items.setdefault(event.session_id, []); event = replace(event, session_seq=len(values) + 1); values.append(event)
        for queue in self.subscribers.get(event.session_id, []): queue.put_nowait(event)
        return event
    async def publish_realtime(self, event: ApplicationEvent) -> ApplicationEvent:
        for queue in self.subscribers.get(event.session_id, []): queue.put_nowait(event)
        return event
    async def open_subscription(self, session_id: str):
        queue: asyncio.Queue[ApplicationEvent] = asyncio.Queue(); self.subscribers.setdefault(session_id, []).append(queue)
        return queue, len(self.items.get(session_id, []))
    async def list_events_between(self, session_id: str, after: int = 0, upto: int | None = None):
        values = self.items.get(session_id, []); return values[after:upto]
    async def close_subscription(self, session_id: str, queue: asyncio.Queue[ApplicationEvent]) -> None:
        if queue in self.subscribers.get(session_id, []): self.subscribers[session_id].remove(queue)


class InMemoryEventPublisher:
    def __init__(self, store: InMemoryEventStore) -> None: self.store = store
    async def required(self, request: Any) -> None: return None
    async def publish(self, event: ApplicationEvent) -> ApplicationEvent: return await self.store.publish(event)
    async def publish_realtime(self, event: ApplicationEvent) -> ApplicationEvent: return await self.store.publish_realtime(event)
