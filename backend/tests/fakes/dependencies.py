"""Build an isolated dependency graph for unit and smoke tests."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from application.approval import ApprovalService
from application.common.query_services import (
    MemoryQueryService,
    ProviderQueryService,
    RetrievalQueryService,
    SettingsQueryService,
)
from application.events import EventStreamService
from application.files import AttachmentService
from application.knowledge import KnowledgeBaseQueryService, KnowledgeBaseService
from application.memory import MemoryService
from application.runs import RunCommandService, RunQueryService
from application.sessions import SessionMessageQueryService, SessionService
from application.tools import MCPService, ToolQueryService, ToolService
from bootstrap import ApplicationDependencies, build_dependencies
from bootstrap.config import get_settings

from .in_memory import (
    InMemoryApprovalEvents,
    InMemoryApprovalRepository,
    InMemoryAttachmentRepository,
    InMemoryDocumentJobs,
    InMemoryEventStore,
    InMemoryFileAdapters,
    InMemoryFileStorage,
    InMemoryKnowledgeDocuments,
    InMemoryKnowledgeRepository,
    InMemoryMCPPort,
    InMemoryMemoryPort,
    InMemoryMessageRepository,
    InMemoryProviderQueries,
    InMemoryRunStore,
    InMemorySessionRepository,
    InMemoryToolExecution,
    InMemoryToolRepository,
)


def build_test_dependencies() -> ApplicationDependencies:
    """Build a stateful in-process graph without external services."""
    sessions = InMemorySessionRepository()
    messages = InMemoryMessageRepository()
    runs = InMemoryRunStore()
    memory = InMemoryMemoryPort()
    tools = InMemoryToolRepository()
    mcp = InMemoryMCPPort()
    approvals = InMemoryApprovalRepository()
    events = InMemoryEventStore()
    storage = InMemoryFileStorage()
    adapters = InMemoryFileAdapters()
    jobs = InMemoryDocumentJobs()
    attachments = InMemoryAttachmentRepository()
    documents = InMemoryKnowledgeDocuments()
    knowledge = InMemoryKnowledgeRepository()
    settings = get_settings()
    queries = InMemoryProviderQueries(
        {
            "providers": [
                {
                    "name": item.name,
                    "provider": item.provider,
                    "model": item.model,
                    "base_url": item.base_url,
                    "api_key_configured": bool(item.api_key),
                    "api_key_masked": "***" if item.api_key else "",
                    "temperature": item.temperature,
                    "max_tokens": item.max_tokens,
                }
                for item in settings.llm_providers
            ]
        },
        memory,
    )
    now_id = lambda: uuid4().hex

    async def settings_payload() -> dict[str, Any]:
        current = get_settings()
        return {"host": current.host, "port": current.port, "debug": current.debug}

    class StorageJob:
        async def enqueue(self, attachment_id: str) -> bool:
            return await jobs.enqueue(attachment_id)

        async def cancel(self, attachment_id: str) -> None:
            await jobs.cancel(attachment_id)

    class KnowledgeDocs:
        async def create(self, value):
            return await documents.create(value)

        async def get(self, key, *, knowledge_base_id):
            return await documents.get(key, knowledge_base_id=knowledge_base_id)

        async def list(self, key):
            return await documents.list(key)

        async def list_versions(self, key, *, knowledge_base_id):
            return await documents.list_versions(key, knowledge_base_id=knowledge_base_id)

        async def latest_for_filename(self, filename, *, knowledge_base_id):
            return await documents.latest_for_filename(filename, knowledge_base_id=knowledge_base_id)

        async def soft_delete(self, key, *, knowledge_base_id):
            return await documents.soft_delete(key, knowledge_base_id=knowledge_base_id)

    providers = {
        "session_service_factory": lambda: SessionService(sessions, id_factory=now_id),
        "message_query_factory": lambda: SessionMessageQueryService(sessions, messages),
        "run_query_factory": lambda: RunQueryService(sessions, runs),
        "run_command_factory": lambda: RunCommandService(sessions, runs, runs, id_factory=now_id),
        "event_stream_factory": lambda: EventStreamService(sessions, events, events),
        "tool_query_factory": lambda: ToolQueryService(tools, queries),
        "tool_service_factory": lambda: ToolService(tools, InMemoryToolExecution()),
        "approval_service_factory": lambda: ApprovalService(approvals, InMemoryApprovalEvents(), id_factory=now_id),
        "mcp_service_factory": lambda: MCPService(mcp),
        "provider_query_factory": lambda: ProviderQueryService(queries),
        "settings_query_factory": lambda: SettingsQueryService(type("SettingsPort", (), {"get_settings": staticmethod(settings_payload)})()),
        "memory_query_factory": lambda: MemoryQueryService(queries),
        "memory_service_factory": lambda: MemoryService(memory),
        "retrieval_query_factory": lambda: RetrievalQueryService(queries),
        "knowledge_query_factory": lambda: KnowledgeBaseQueryService(knowledge),
        "attachment_service_factory": lambda: AttachmentService(sessions, attachments, storage, adapters, StorageJob(), id_factory=now_id),
        "knowledge_service_factory": lambda: KnowledgeBaseService(knowledge, KnowledgeDocs(), storage, adapters, StorageJob(), id_factory=now_id),
    }
    return build_dependencies(factories=providers)


__all__ = ["build_test_dependencies"]
