"""Production dependency graph backed by the target PostgreSQL adapters."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from application.approval import ApprovalService
from application.common.query_services import (
    ProviderQueryService,
    RetrievalQueryService,
    SettingsQueryService,
)
from application.events import EventStreamService
from application.files import AttachmentService, IngestionService, KnowledgeDocumentWorker
from application.knowledge import KnowledgeBaseQueryService, KnowledgeBaseService
from application.memory import MemoryService
from application.runs import RunCommandService, RunQueryService
from application.runs import CommandConsumer, RunExecutionService
from application.sessions import SessionMessageQueryService, SessionService
from application.tools import MCPService, ToolQueryService, ToolService
from application.orchestration import OrchestrationService
from bootstrap.config import Settings
from domain.approval import ApprovalRequest
from domain.events import ApplicationEvent, EventDurability, EventType
from domain.integrations.ports import SandboxPort
from domain.tools import MCPPort, MCPServerConfig, ToolExecutionPort, ToolExecutionRequest, ToolExecutionResult
from infrastructure.integrations.file_parsers.native import (
    NativeFileAdapterRegistry,
    NativeTextParser,
    line_chunker,
)
from infrastructure.integrations.llm.provider import ConfiguredEmbeddingProvider, ConfiguredLLMProvider
from infrastructure.integrations.neo4j.resource import Neo4jResource
from infrastructure.integrations.pgvector import PostgresFileVectorIndexer
from infrastructure.integrations.sandbox.docker_runner import DockerSandbox
from infrastructure.tools.native import NativeToolExecution
from infrastructure.integrations.storage.local import LocalFileStorageAdapter
from infrastructure.persistence.postgres.engine import PostgresResource
from infrastructure.persistence.postgres.repositories import (
    PostgresApprovalRepository,
    PostgresAttachmentRepository,
    PostgresDocumentJobRepository,
    PostgresEventRepository,
    PostgresKnowledgeBaseRepository,
    PostgresKnowledgeDocumentRepository,
    PostgresMCPServerRepository,
    PostgresMemoryRepository,
    PostgresMessageRepository,
    PostgresRetrievalQueryRepository,
    PostgresRunRepository,
    PostgresSessionRepository,
    PostgresToolConfigRepository,
    PostgresOrchestrationRepository,
)
from workers import WorkerSupervisor

from .dependencies import ApplicationDependencies, Provider, build_dependencies


class PostgresSettingsQueries:
    """Read-only provider and settings queries for the target graph."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def list_providers(self) -> list[dict[str, Any]]:
        return [
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
            for item in self._settings.llm_providers
        ]

    async def get_settings(self) -> dict[str, Any]:
        return {
            "host": self._settings.host,
            "port": self._settings.port,
            "debug": self._settings.debug,
            "database_backend": self._settings.database_backend,
            "workers_enabled": self._settings.workers_enabled,
        }


class PostgresApprovalEvents:
    """Project approval requests onto the durable target event store."""

    def __init__(self, events: PostgresEventRepository) -> None:
        self._events = events

    async def required(self, request: ApprovalRequest) -> None:
        await self._events.publish(
            ApplicationEvent(
                event_type=EventType.APPROVAL_REQUIRED,
                durability=EventDurability.DURABLE,
                session_id=request.session_id,
                run_id=request.run_id,
                payload={
                    "approval_id": request.approval_id,
                    "tool_name": request.tool_name,
                    "risk_level": request.risk_level,
                },
            )
        )


class PostgresMCPPort(MCPPort):
    """Persist MCP registrations without coupling application code to SQLAlchemy."""

    def __init__(self, repository: PostgresMCPServerRepository) -> None:
        self._repository = repository

    async def register(self, name: str, config: MCPServerConfig) -> dict[str, object]:
        from datetime import timezone
        from domain.tools import MCPServer

        await self._repository.upsert(
            MCPServer(name, config, datetime.now(timezone.utc))
        )
        return {"name": name, "status": "registered", "enabled": config.enabled}

    async def unregister(self, name: str) -> None:
        await self._repository.delete(name)

    async def list_servers(self) -> list[dict[str, object]]:
        values = await self._repository.list_all()
        return [
            {"name": item.name, "status": "registered", "enabled": item.config.enabled}
            for item in values
        ]

    async def shutdown(self) -> None:
        return None


class PostgresRunExecutor:
    """Explicit execution boundary for the target graph.

    The target command path has deterministic completion semantics while the
    richer graph is assembled by a deployment-specific RootGraphPort.
    """

    async def execute(self, command):
        from domain.runs import CommandExecutionResult
        if command.command_type.value == "run.start":
            return CommandExecutionResult(result={"status": "accepted", "message": command.payload.get("message", "")})
        return CommandExecutionResult(result={"status": "accepted"})

    async def cancel(self, run_id: str | None) -> None:
        return None


def build_postgres_dependencies(settings: Settings) -> ApplicationDependencies:
    """Assemble the complete target graph for a PostgreSQL deployment.

    No connection is opened here.  ``PostgresResource`` owns startup and is
    registered as a lifespan resource so construction remains safe for CLI
    inspection and dependency tests.
    """

    postgres = PostgresResource(settings, create_schema=settings.postgres_create_schema)
    session_factory = postgres.session
    sessions = PostgresSessionRepository(session_factory)
    messages = PostgresMessageRepository(session_factory)
    runs = PostgresRunRepository(session_factory)
    events = PostgresEventRepository(session_factory)
    approvals = PostgresApprovalRepository(session_factory)
    memory = PostgresMemoryRepository(session_factory)
    retrieval = PostgresRetrievalQueryRepository(session_factory)
    tools = PostgresToolConfigRepository(session_factory)
    mcp_repository = PostgresMCPServerRepository(session_factory)
    orchestration = PostgresOrchestrationRepository(session_factory)
    embedding = ConfiguredEmbeddingProvider(settings)
    neo4j = Neo4jResource(settings)
    file_vectors = PostgresFileVectorIndexer(session_factory, embedding)
    sandbox = DockerSandbox(settings)
    mcp = PostgresMCPPort(mcp_repository)
    attachments = PostgresAttachmentRepository(session_factory)
    knowledge = PostgresKnowledgeBaseRepository(session_factory)
    documents = PostgresKnowledgeDocumentRepository(session_factory)
    jobs = PostgresDocumentJobRepository(session_factory)
    storage = LocalFileStorageAdapter(settings.files_path, settings.file_max_upload_bytes)
    adapters = NativeFileAdapterRegistry()
    parser = NativeTextParser(storage)
    ingestion = IngestionService(
        attachments,
        # Ingestion uses the same attachment table for its chunk owner; the
        # concrete chunk repository is provided by the target repository set.
        _PostgresChunkRepository(session_factory),
        parser,
        file_vectors,
        _LazyGraphIndexer(neo4j),
        chunker=line_chunker(max_characters=settings.file_chunk_tokens * 4),
    )
    settings_queries = PostgresSettingsQueries(settings)
    ids = __import__("uuid").uuid4

    async def settings_payload() -> dict[str, Any]:
        return await settings_queries.get_settings()

    providers: dict[str, Provider] = {
        "session_service_factory": lambda: SessionService(sessions, id_factory=lambda: ids().hex),
        "message_query_factory": lambda: SessionMessageQueryService(sessions, messages),
        "run_query_factory": lambda: RunQueryService(sessions, runs),
        "run_command_factory": lambda: RunCommandService(sessions, runs, runs, id_factory=lambda: ids().hex),
        "event_stream_factory": lambda: EventStreamService(sessions, events, events),
        "tool_query_factory": lambda: ToolQueryService(tools),
        "tool_service_factory": lambda: ToolService(tools, NativeToolExecution(settings.files_path, max_output_bytes=settings.sandbox_max_output_bytes)),
        "approval_service_factory": lambda: ApprovalService(approvals, PostgresApprovalEvents(events), id_factory=lambda: ids().hex),
        "mcp_service_factory": lambda: MCPService(mcp),
        "provider_query_factory": lambda: ProviderQueryService(settings_queries),
        "settings_query_factory": lambda: SettingsQueryService(type("SettingsPort", (), {"get_settings": staticmethod(settings_payload)})()),
        "memory_service_factory": lambda: MemoryService(memory),
        "retrieval_query_factory": lambda: RetrievalQueryService(retrieval),
        "knowledge_query_factory": lambda: KnowledgeBaseQueryService(knowledge),
        "attachment_service_factory": lambda: AttachmentService(sessions, attachments, storage, adapters, jobs, id_factory=lambda: ids().hex),
        "knowledge_service_factory": lambda: KnowledgeBaseService(knowledge, documents, storage, adapters, jobs, id_factory=lambda: ids().hex),
        "orchestration_repository_factory": lambda: orchestration,
        "orchestration_service_factory": lambda: OrchestrationService(orchestration),
    }
    # Keep the ingestion service visible to deployments that provide a worker
    # process; it is deliberately not started unless workers_enabled is true.
    providers["ingestion_service_factory"] = lambda: ingestion
    resources: list[object] = [postgres, neo4j]
    providers["llm_provider"] = lambda: ConfiguredLLMProvider(settings)
    providers["embedding_provider"] = lambda: embedding
    providers["sandbox"] = lambda: sandbox
    providers["retrieval_service_factory"] = lambda: __import__("application.retrieval", fromlist=["HybridRetrievalService"]).HybridRetrievalService()
    providers["context_service_factory"] = lambda: __import__("application.runs.context", fromlist=["ContextAcquisitionService"]).ContextAcquisitionService({})
    providers["task_understanding_factory"] = lambda: __import__("application.runs.task_understanding", fromlist=["TaskUnderstandingService"]).TaskUnderstandingService()
    resources.append(sandbox)
    if settings.workers_enabled:
        command_execution = RunExecutionService(runs, runs, PostgresRunExecutor(), events)
        command_worker = CommandConsumer(runs, command_execution)
        document_worker = KnowledgeDocumentWorker(jobs, ingestion)
        resources.append(WorkerSupervisor((command_worker, document_worker)))
    return build_dependencies(factories=providers, resources=resources)


class _PostgresChunkRepository:
    """Small local adapter kept private to the bootstrap graph."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def replace_for_attachment(self, attachment_id, chunks):
        from infrastructure.persistence.postgres.repositories.file_repository import PostgresFileChunkRepository

        await PostgresFileChunkRepository(self._session_factory).replace_for_attachment(attachment_id, chunks)


class _LazyGraphIndexer:
    def __init__(self, resource: Neo4jResource) -> None:
        self._resource = resource

    async def index(self, attachment, chunks) -> None:
        if self._resource.adapter is not None:
            await self._resource.adapter.index_attachment(attachment, chunks)

    async def delete(self, attachment_id: str) -> None:
        if self._resource.adapter is not None:
            await self._resource.adapter.delete_attachment(attachment_id)


__all__ = ["PostgresApprovalEvents", "PostgresMCPPort", "build_postgres_dependencies"]
