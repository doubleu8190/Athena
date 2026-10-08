"""Production dependency graph backed by the target PostgreSQL adapters."""

from __future__ import annotations

from application.approval import ApprovalService
from application.configuration import ProviderQueryService, SettingsQueryService
from application.events import EventStreamService
from application.files import AttachmentService, IngestionService
from application.knowledge import KnowledgeBaseQueryService, KnowledgeBaseService
from application.memory import MemoryService
from application.retrieval import RetrievalQueryService
from application.runs import RunCommandService, RunQueryService
from application.runs import RunExecutionService
from application.sessions import SessionMessageQueryService, SessionService
from application.tools import MCPService, ToolQueryService, ToolService
from application.orchestration import OrchestrationService, WorkerSchedulerService
from application.runs.context import (
    ContextAcquisitionService,
    ContextPlan,
    ContextPlanner,
)
from application.files.retrieval_service import (
    FileContextProvider,
    KnowledgeContextProvider,
)
from application.memory.retrieval_service import MemoryContextProvider
from application.retrieval.graph_service import GraphContextProvider
from application.runs.task_understanding import TaskUnderstandingService
from configuration import Settings
from shared import monotonic_timestamp_id_factory
from infrastructure.integrations.file_parsers.parser import (
    NativeFileAdapterRegistry,
    NativeFileParser,
    fixed_size_chunker,
)
from infrastructure.integrations.llm.embedding_provider import (
    ConfiguredEmbeddingProvider,
)
from infrastructure.integrations.llm.llm_provider import (
    ConfiguredLLMProvider,
)
from infrastructure.integrations.llm.options import (
    EmbeddingOptions,
    LLMOptions,
    LLMProviderOptions,
    RetryOptions,
)
from infrastructure.integrations.neo4j.resource import Neo4jResource
from infrastructure.integrations.neo4j.options import Neo4jOptions
from infrastructure.integrations.neo4j.file_indexer import (
    OptionalNeo4jFileIndexer,
)
from infrastructure.integrations.pgvector.indexers import (
    PostgresFileVectorIndexer,
    PostgresMemoryVectorIndexer,
)
from infrastructure.integrations.sandbox.docker import DockerSandbox
from infrastructure.integrations.sandbox.options import SandboxOptions
from infrastructure.tools.executor import BuiltinToolExecutor
from infrastructure.integrations.storage.storage import LocalFileStorage
from infrastructure.orchestration.langgraph import (
    ConfiguredRootAgent,
    LangGraphRootGraph,
    LangGraphRunExecutor,
    LangGraphWorkerExecutor,
    RootGraphDependencies,
)
from infrastructure.configuration import (
    ConfiguredSettingsQueries,
    PublicConfiguration,
    PublicRuntimeOptions,
)
from infrastructure.persistence.postgres.resource import PostgresEngine
from infrastructure.persistence.postgres.options import PostgresOptions
from infrastructure.approval import ApprovalEventPublisher
from infrastructure.persistence.postgres.adapters import (
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
    PostgresRetrievalRepository,
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

    No connection is opened here.  ``PostgresEngine`` owns startup and is
    registered as a lifespan resource so construction remains safe for CLI
    inspection and dependency tests.
    """

    postgres = PostgresEngine(
        PostgresOptions(
            url=settings.postgres_url,
            pool_size=settings.postgres_pool_size,
            max_overflow=settings.postgres_max_overflow,
            create_schema=settings.postgres_create_schema,
        )
    )
    session_factory = postgres.session
    sessions = PostgresSessionRepository(session_factory)
    messages = PostgresMessageRepository(session_factory)
    runs = PostgresRunRepository(session_factory)
    events = PostgresEventRepository(session_factory)
    approvals = PostgresApprovalRepository(session_factory)
    memory = PostgresMemoryRepository(session_factory)
    retrieval = PostgresRetrievalRepository(session_factory)
    tools = PostgresToolConfigRepository(session_factory)
    mcp_repository = PostgresMCPServerRepository(session_factory)
    orchestration = PostgresOrchestrationRepository(session_factory)
    llm_options = LLMOptions(
        providers=tuple(
            LLMProviderOptions(
                name=item.name,
                provider=item.provider,
                model=item.model,
                api_key=item.api_key,
                base_url=item.base_url,
                temperature=item.temperature,
                max_tokens=item.max_tokens,
            )
            for item in settings.llm_providers
        ),
        retry=RetryOptions(
            attempts=settings.llm_retry.max_attempts,
            timeout_seconds=settings.llm_retry.timeout_ms / 1000,
            delay_seconds=settings.llm_retry.min_delay_ms / 1000,
        ),
    )
    embedding_options = EmbeddingOptions(
        provider=settings.embedding_provider,
        model=settings.embedding_model,
        device=settings.embedding_device,
        dimension=settings.embedding_dimension,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
    )
    embedding = ConfiguredEmbeddingProvider(embedding_options)
    neo4j = Neo4jResource(
        Neo4jOptions(
            uri=settings.neo4j_uri,
            username=settings.neo4j_username,
            password=settings.neo4j_password,
            database=settings.neo4j_database,
            max_connection_pool_size=settings.neo4j_max_connection_pool_size,
            connection_timeout_seconds=settings.neo4j_connection_timeout_seconds,
        )
    )
    file_vectors = PostgresFileVectorIndexer(session_factory, embedding)
    memory_vectors = PostgresMemoryVectorIndexer(session_factory, embedding)
    sandbox = DockerSandbox(
        SandboxOptions(
            docker_binary=settings.sandbox_docker_binary,
            default_network=settings.sandbox_default_network,
            max_output_bytes=settings.sandbox_max_output_bytes,
            memory_mb=settings.sandbox_memory_mb,
            cpu_limit=settings.sandbox_cpu_limit,
        )
    )
    mcp = PostgresMCPAdapter(mcp_repository)
    attachments = PostgresAttachmentRepository(session_factory)
    knowledge = PostgresKnowledgeBaseRepository(session_factory)
    documents = PostgresKnowledgeDocumentRepository(session_factory)
    jobs = PostgresDocumentJobRepository(session_factory)
    storage = LocalFileStorage(settings.files_path, settings.file_max_upload_bytes)
    adapters = NativeFileAdapterRegistry()
    parser = NativeFileParser(storage)
    chunks = PostgresFileChunkRepository(session_factory)
    ingestion = IngestionService(
        attachments,
        # Ingestion uses the same attachment table for its chunk owner; the
        # concrete chunk repository is provided by the target repository set.
        chunks,
        parser,
        file_vectors,
        OptionalNeo4jFileIndexer(neo4j),
        chunker=fixed_size_chunker(max_characters=settings.file_chunk_tokens * 4),
    )
    settings_queries = ConfiguredSettingsQueries(
        PublicConfiguration(
            providers=tuple(
                LLMProviderOptions(
                    name=item.name,
                    provider=item.provider,
                    model=item.model,
                    api_key=item.api_key,
                    base_url=item.base_url,
                    temperature=item.temperature,
                    max_tokens=item.max_tokens,
                )
                for item in settings.llm_providers
            ),
            runtime=PublicRuntimeOptions(
                host=settings.host,
                port=settings.port,
                debug=settings.debug,
                database_backend=settings.database_backend,
                workers_enabled=settings.workers_enabled,
            ),
        )
    )
    ids = monotonic_timestamp_id_factory()
    llm_provider = ConfiguredLLMProvider(llm_options)
    task_understanding = TaskUnderstandingService(
        llm_provider, timeout_seconds=settings.task_understanding_timeout_seconds
    )
    context_planner = ContextPlanner(
        max_files=settings.knowledge_context_max_files,
        limit_per_file=settings.knowledge_context_limit_per_file,
        max_items=settings.knowledge_context_max_items,
        max_tokens=settings.knowledge_context_max_tokens,
        recall_limit=settings.retrieval_candidate_k,
        rerank_limit=settings.retrieval_rerank_k,
    )
    context_service = ContextAcquisitionService(
        providers={
            "memory": MemoryContextProvider(
                memory,
                memory_vectors,
                limit=settings.retrieval_context_k,
                timeout_seconds=settings.memory_retrieval_timeout_seconds,
            ),
            "file": FileContextProvider(
                chunks, file_vectors, limit=settings.knowledge_context_limit_per_file
            ),
            "knowledge": KnowledgeContextProvider(
                chunks,
                attachments,
                file_vectors,
                limit=settings.knowledge_context_limit_per_file,
            ),
            "graph": GraphContextProvider(
                neo4j,
                limit=settings.graph_path_limit,
                timeout_seconds=settings.graph_timeout_seconds,
            ),
        },
        trace_repository=retrieval,
    )
    root_graph = LangGraphRootGraph(
        dependencies=RootGraphDependencies(
            task_understanding=task_understanding,
            context=context_service,
            context_planner=context_planner,
            context_plan_factory=lambda value: ContextPlan(**value),
            orchestration=OrchestrationService(orchestration),
            plan_repository=orchestration,
            scheduler=WorkerSchedulerService(orchestration, LangGraphWorkerExecutor()),
            agent=ConfiguredRootAgent(llm_provider),
            event_publisher=events,
        )
    )

    providers: dict[str, Provider] = {
        "session_service_factory": lambda: SessionService(sessions, id_factory=ids),
        "message_query_factory": lambda: SessionMessageQueryService(sessions, messages),
        "run_query_factory": lambda: RunQueryService(sessions, runs),
        "run_command_factory": lambda: RunCommandService(
            sessions, runs, runs, id_factory=ids
        ),
        "event_stream_factory": lambda: EventStreamService(sessions, events, events),
        "tool_query_factory": lambda: ToolQueryService(tools),
        "tool_service_factory": lambda: ToolService(
            tools,
            BuiltinToolExecutor(
                settings.files_path, max_output_bytes=settings.sandbox_max_output_bytes
            ),
        ),
        "approval_service_factory": lambda: ApprovalService(
            approvals, ApprovalEventPublisher(events), id_factory=ids
        ),
        "mcp_service_factory": lambda: MCPService(mcp),
        "provider_query_factory": lambda: ProviderQueryService(settings_queries),
        "settings_query_factory": lambda: SettingsQueryService(settings_queries),
        "memory_service_factory": lambda: MemoryService(memory),
        "retrieval_query_factory": lambda: RetrievalQueryService(retrieval),
        "knowledge_query_factory": lambda: KnowledgeBaseQueryService(knowledge),
        "attachment_service_factory": lambda: AttachmentService(
            sessions, attachments, storage, adapters, jobs, id_factory=ids
        ),
        "knowledge_service_factory": lambda: KnowledgeBaseService(
            knowledge, documents, storage, adapters, jobs, id_factory=ids
        ),
        "orchestration_repository_factory": lambda: orchestration,
        "orchestration_service_factory": lambda: OrchestrationService(orchestration),
        "root_graph_factory": lambda: root_graph,
        "run_executor_factory": lambda: LangGraphRunExecutor(root_graph),
        "ingestion_service_factory": lambda: ingestion,
        "llm_provider": lambda: llm_provider,
        "embedding_provider": lambda: embedding,
        "sandbox": lambda: sandbox,
        "retrieval_service_factory": lambda: __import__(
            "application.retrieval", fromlist=["HybridRetrievalService"]
        ).HybridRetrievalService(),
        "context_service_factory": lambda: context_service,
        "task_understanding_factory": lambda: task_understanding,
    }
    resources: list[object] = [postgres, neo4j]
    resources.append(sandbox)
    if settings.workers_enabled:
        command_execution = RunExecutionService(
            runs, runs, LangGraphRunExecutor(root_graph), events
        )
        command_worker = CommandConsumer(runs, command_execution)
        document_worker = KnowledgeDocumentWorker(jobs, ingestion)
        resources.append(WorkerSupervisor((command_worker, document_worker)))
    return build_dependencies(factories=providers, resources=resources)


__all__ = ["build_postgres_dependencies"]
