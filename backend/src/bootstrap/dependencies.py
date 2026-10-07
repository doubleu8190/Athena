"""Explicit dependency graph for the restructured application.

The bootstrap layer owns assembly.  Domain and application packages only see
ports, while this module keeps factories and process resources in one place.
The default graph is intentionally empty so importing the application never
opens a database connection or starts a worker.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


Provider = Callable[[], Any]


@dataclass(frozen=True)
class ApplicationDependencies:
    """The process-level dependency graph consumed by ``bootstrap.app``.

    ``factories`` contains application service providers keyed by the names
    accepted by :func:`create_app`.  ``resources`` are started before the app
    accepts requests and stopped in reverse order during shutdown.
    """

    factories: Mapping[str, Provider] = field(default_factory=dict)
    resources: Sequence[Any] = field(default_factory=tuple)
    routers: Sequence[Any] = field(default_factory=tuple)

    def provider(self, name: str) -> Provider | None:
        """Return a registered provider, if the graph contains one."""
        return self.factories.get(name)


def build_dependencies(
    *,
    factories: Mapping[str, Provider] | None = None,
    resources: Sequence[Any] = (),
    routers: Sequence[Any] = (),
) -> ApplicationDependencies:
    """Create an immutable dependency graph from bootstrap inputs."""
    return ApplicationDependencies(
        factories=dict(factories or {}),
        resources=tuple(resources),
        routers=tuple(routers),
    )


def build_default_dependencies() -> ApplicationDependencies:
    """Build a complete local dependency graph for the target application.

    The graph is deliberately backed by process-local adapters when no external
    database is configured.  Deployments can replace these providers with the
    PostgreSQL/integration providers while retaining the same application and
    HTTP contracts.
    """
    import os

    from bootstrap.config import get_settings

    settings = get_settings()
    if settings.database_backend == "postgres" or os.getenv("ATHENA_DATABASE_BACKEND", "").lower() == "postgres":
        from .postgres import build_postgres_dependencies

        if os.getenv("ATHENA_WORKERS_ENABLED", "").lower() in {"1", "true", "yes", "on"}:
            settings.workers_enabled = True
        return build_postgres_dependencies(settings)

    from application.approval import ApprovalService
    from application.common.query_services import (
        MemoryQueryService, ProviderQueryService, RetrievalQueryService, SettingsQueryService,
    )
    from application.events import EventStreamService
    from application.files import AttachmentService
    from application.knowledge import KnowledgeBaseQueryService, KnowledgeBaseService
    from application.memory import MemoryService
    from application.runs import RunCommandService, RunQueryService
    from application.sessions import SessionMessageQueryService, SessionService
    from application.tools import MCPService, ToolQueryService, ToolService
    from infrastructure.in_memory import (
        InMemoryApprovalEvents, InMemoryApprovalRepository, InMemoryAttachmentRepository,
        InMemoryDocumentJobs, InMemoryEventStore, InMemoryFileAdapters, InMemoryFileStorage,
        InMemoryKnowledgeDocuments, InMemoryKnowledgeRepository, InMemoryMCPPort,
        InMemoryMemoryPort, InMemoryMessageRepository, InMemoryProviderQueries,
        InMemoryRunStore, InMemorySessionRepository, InMemoryToolExecution, InMemoryToolRepository,
    )

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
    queries = InMemoryProviderQueries(
        {"providers": [{"name": item.name, "provider": item.provider, "model": item.model, "base_url": item.base_url, "api_key_configured": bool(item.api_key), "api_key_masked": "***" if item.api_key else "", "temperature": item.temperature, "max_tokens": item.max_tokens} for item in get_settings().llm_providers]},
        memory,
    )
    now_id = lambda: uuid4().hex
    async def settings_payload() -> dict[str, Any]:
        settings = get_settings()
        return {"host": settings.host, "port": settings.port, "debug": settings.debug}

    class _StorageJob:
        async def enqueue(self, attachment_id: str) -> bool: return await jobs.enqueue(attachment_id)
        async def cancel(self, attachment_id: str) -> None: await jobs.cancel(attachment_id)

    class _KnowledgeDocs:
        async def create(self, value): return await documents.create(value)
        async def get(self, key, *, knowledge_base_id): return await documents.get(key, knowledge_base_id=knowledge_base_id)
        async def list(self, key): return await documents.list(key)
        async def list_versions(self, key, *, knowledge_base_id): return await documents.list_versions(key, knowledge_base_id=knowledge_base_id)
        async def latest_for_filename(self, filename, *, knowledge_base_id): return await documents.latest_for_filename(filename, knowledge_base_id=knowledge_base_id)
        async def soft_delete(self, key, *, knowledge_base_id): return await documents.soft_delete(key, knowledge_base_id=knowledge_base_id)

    providers: dict[str, Provider] = {
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
        "attachment_service_factory": lambda: AttachmentService(sessions, attachments, storage, adapters, _StorageJob(), id_factory=now_id),
        "knowledge_service_factory": lambda: KnowledgeBaseService(knowledge, _KnowledgeDocs(), storage, adapters, _StorageJob(), id_factory=now_id),
    }
    return build_dependencies(factories=providers, resources=())


__all__ = ["ApplicationDependencies", "Provider", "build_dependencies", "build_default_dependencies"]
