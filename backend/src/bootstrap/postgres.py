"""Production dependency graph backed by the target PostgreSQL adapters."""

from __future__ import annotations

from application.approval import ApprovalService
from application.common.query_services import (
    ProviderQueryService,
    RetrievalQueryService,
    SettingsQueryService,
)
from application.events import EventStreamService
from application.files import AttachmentService, IngestionService
from application.knowledge import KnowledgeBaseQueryService, KnowledgeBaseService
from application.memory import MemoryService
from application.runs import RunCommandService, RunQueryService
from application.runs import RunExecutionService
from application.sessions import SessionMessageQueryService, SessionService
from application.tools import MCPService, ToolQueryService, ToolService
from application.orchestration import OrchestrationService, WorkerSchedulerService
from application.runs.context import ContextAcquisitionService
from application.runs.task_understanding import TaskUnderstandingService
from bootstrap.config import Settings
from shared import monotonic_timestamp_id_factory
from infrastructure.integrations.file_parsers.native import (
    NativeFileAdapterRegistry,
    NativeTextParser,
    line_chunker,
)
from infrastructure.integrations.llm.provider import ConfiguredEmbeddingProvider, ConfiguredLLMProvider
from infrastructure.integrations.neo4j.resource import Neo4jResource
from infrastructure.integrations.neo4j.attachment_indexer import OptionalNeo4jAttachmentIndexer
from infrastructure.integrations.pgvector import PostgresFileVectorIndexer
from infrastructure.integrations.sandbox.docker_runner import DockerSandbox
from infrastructure.tools.native import NativeToolExecution
from infrastructure.integrations.storage.local import LocalFileStorageAdapter
from infrastructure.orchestration.langgraph import (
    ConfiguredRootAgent,
    LangGraphRootGraph,
    LangGraphRunExecutor,
    LangGraphWorkerExecutor,
    RootGraphDependencies,
)
from infrastructure.configuration import ConfiguredSettingsQueries
from infrastructure.persistence.postgres.engine import PostgresResource
from infrastructure.persistence.postgres.adapters import (
    PostgresApprovalEventPublisher,
    PostgresMCPAdapter,
)
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
    PostgresFileChunkRepository,
    PostgresOrchestrationRepository,
)
from workers import CommandConsumer, KnowledgeDocumentWorker, WorkerSupervisor

from .dependencies import ApplicationDependencies, Provider, build_dependencies


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
    mcp = PostgresMCPAdapter(mcp_repository)
    attachments = PostgresAttachmentRepository(session_factory)
    knowledge = PostgresKnowledgeBaseRepository(session_factory)
    documents = PostgresKnowledgeDocumentRepository(session_factory)
    jobs = PostgresDocumentJobRepository(session_factory)
    storage = LocalFileStorageAdapter(settings.files_path, settings.file_max_upload_bytes)
    adapters = NativeFileAdapterRegistry()
    parser = NativeTextParser(storage)
    chunks = PostgresFileChunkRepository(session_factory)
    ingestion = IngestionService(
        attachments,
        # Ingestion uses the same attachment table for its chunk owner; the
        # concrete chunk repository is provided by the target repository set.
        chunks,
        parser,
        file_vectors,
        OptionalNeo4jAttachmentIndexer(neo4j),
        chunker=line_chunker(max_characters=settings.file_chunk_tokens * 4),
    )
    settings_queries = ConfiguredSettingsQueries(settings)
    ids = monotonic_timestamp_id_factory()
    llm_provider = ConfiguredLLMProvider(settings)
    task_understanding = TaskUnderstandingService()
    context_service = ContextAcquisitionService({})
    worker_scheduler = WorkerSchedulerService(orchestration, LangGraphWorkerExecutor())
    root_graph = LangGraphRootGraph(
        dependencies=RootGraphDependencies(
            task_understanding=task_understanding,
            context=context_service,
            orchestration=OrchestrationService(orchestration),
            plan_repository=orchestration,
            scheduler=worker_scheduler,
            agent=ConfiguredRootAgent(llm_provider),
            event_publisher=events,
        )
    )

    providers: dict[str, Provider] = {
        "session_service_factory": lambda: SessionService(sessions, id_factory=ids),
        "message_query_factory": lambda: SessionMessageQueryService(sessions, messages),
        "run_query_factory": lambda: RunQueryService(sessions, runs),
        "run_command_factory": lambda: RunCommandService(sessions, runs, runs, id_factory=ids),
        "event_stream_factory": lambda: EventStreamService(sessions, events, events),
        "tool_query_factory": lambda: ToolQueryService(tools),
        "tool_service_factory": lambda: ToolService(tools, NativeToolExecution(settings.files_path, max_output_bytes=settings.sandbox_max_output_bytes)),
        "approval_service_factory": lambda: ApprovalService(approvals, PostgresApprovalEventPublisher(events), id_factory=ids),
        "mcp_service_factory": lambda: MCPService(mcp),
        "provider_query_factory": lambda: ProviderQueryService(settings_queries),
        "settings_query_factory": lambda: SettingsQueryService(settings_queries),
        "memory_service_factory": lambda: MemoryService(memory),
        "retrieval_query_factory": lambda: RetrievalQueryService(retrieval),
        "knowledge_query_factory": lambda: KnowledgeBaseQueryService(knowledge),
        "attachment_service_factory": lambda: AttachmentService(sessions, attachments, storage, adapters, jobs, id_factory=ids),
        "knowledge_service_factory": lambda: KnowledgeBaseService(knowledge, documents, storage, adapters, jobs, id_factory=ids),
        "orchestration_repository_factory": lambda: orchestration,
        "orchestration_service_factory": lambda: OrchestrationService(orchestration),
        "root_graph_factory": lambda: root_graph,
        "run_executor_factory": lambda: LangGraphRunExecutor(root_graph),
    }
    # Keep the ingestion service visible to deployments that provide a worker
    # process; it is deliberately not started unless workers_enabled is true.
    providers["ingestion_service_factory"] = lambda: ingestion
    resources: list[object] = [postgres, neo4j]
    providers["llm_provider"] = lambda: llm_provider
    providers["embedding_provider"] = lambda: embedding
    providers["sandbox"] = lambda: sandbox
    providers["retrieval_service_factory"] = lambda: __import__("application.retrieval", fromlist=["HybridRetrievalService"]).HybridRetrievalService()
    providers["context_service_factory"] = lambda: context_service
    providers["task_understanding_factory"] = lambda: task_understanding
    resources.append(sandbox)
    if settings.workers_enabled:
        command_execution = RunExecutionService(runs, runs, LangGraphRunExecutor(root_graph), events)
        command_worker = CommandConsumer(runs, command_execution)
        document_worker = KnowledgeDocumentWorker(jobs, ingestion)
        resources.append(WorkerSupervisor((command_worker, document_worker)))
    return build_dependencies(factories=providers, resources=resources)


__all__ = ["build_postgres_dependencies"]
