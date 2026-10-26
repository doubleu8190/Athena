"""Production dependency graph backed by the target PostgreSQL adapters."""

from __future__ import annotations

from pathlib import Path

from application.approval import ApprovalService
from application.configuration import ProviderQueryService, SettingsQueryService
from application.events import EventStreamService
from application.files import AttachmentService, IngestionService
from application.knowledge import KnowledgeBaseQueryService, KnowledgeBaseService
from application.memory import (
    DefaultMemoryWriteTrigger,
    LLMFactExtractor,
    LLMMemoryEpisodeSummarizer,
    LLMMemoryResolver,
    MemoryEpisodeManagementService,
    MemoryProjectSpaceService,
    MemoryService,
    SessionMemorySettingsService,
    MemoryWriteWorkflow,
)
from application.retrieval import RetrievalQueryService
from application.runs import (
    RunCommandService,
    RunExecutionService,
    RunQueryService,
    RunRequestPreparationService,
)
from application.sessions import SessionMessageQueryService, SessionService
from application.tools import MCPService, ToolQueryService, ToolService
from application.orchestration import (
    OrchestrationCommandHandler,
    RootWorkflow,
    RootWorkflowDependencies,
    WorkerApprovalService,
    WorkerToolExecutor,
    WorkerResultStore,
    WorkerWorkflow,
    OrchestrationQueryService,
)
from application.orchestration.answer_generation import (
    AnswerGenerationService,
    AnswerStreamingOptions,
)
from application.runs.context import (
    ContextAcquisitionService,
    ContextPlanner,
)
from application.files.retrieval_service import (
    FileContextProvider,
    KnowledgeContextProvider,
)
from application.memory.retrieval_service import MemoryContextProvider
from application.memory.episode_retrieval_service import MemoryEpisodeContextProvider
from application.memory.job_service import MemoryJobManagementService
from application.retrieval.graph_service import GraphContextProvider
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
    PostgresFileVectorIndex,
    PostgresMemoryVectorIndex,
)
from infrastructure.integrations.sandbox.docker import DockerSandbox
from infrastructure.integrations.sandbox.options import SandboxOptions
from infrastructure.tools import BuiltinToolExecutor
from infrastructure.integrations.storage.storage import LocalFileStorage
from application.orchestration.langgraph import (
    LangGraphRootGraph,
    LangGraphRunExecutor,
    LangGraphWorkerExecutor,
)
from application.orchestration.agents import ConfiguredRootAgent, ConfiguredWorkerAgent
from infrastructure.configuration import (
    ConfiguredSettingsQueries,
    PublicConfiguration,
    PublicRuntimeOptions,
)
from infrastructure.persistence.postgres.resource import (
    PostgresCheckpointer,
    PostgresEngine,
)
from infrastructure.persistence.postgres.graph_thread_lock import (
    PostgresGraphThreadLock,
)
from infrastructure.persistence.postgres.options import PostgresOptions
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
    PostgresMemoryJobRepository,
    PostgresMemoryOperationsRepository,
    PostgresMemoryEpisodeRepository,
    PostgresMemoryProjectSpaceRepository,
    PostgresMemoryRepository,
    PostgresMessageRepository,
    PostgresRetrievalRepository,
    PostgresRunRepository,
    PostgresSessionRepository,
    PostgresSessionMemorySettingsRepository,
    PostgresToolConfigRepository,
    PostgresFileChunkRepository,
    PostgresOrchestrationRepository,
    PostgresOrchestrationCommandRepository,
    PostgresOrchestrationEventRepository,
    PostgresWorkerJobRepository,
    PostgresWorkerToolOperationRepository,
    PostgresWorkerExecutionMessageRepository,
    PostgresRootResumeJobRepository,
    PostgresOrchestrationOutboxRepository,
    PostgresPlanLoader,
    PostgresEvaluationRepository,
)
from infrastructure.evaluation.retrieval_target import EmbeddingRetrievalTarget
from infrastructure.evaluation import EvaluationEventPublisher, load_dataset
from application.evaluation import EvaluationProgressHub
from workers import (
    CommandConsumer,
    KnowledgeDocumentWorker,
    MemoryWorker,
    WorkerJobDispatcher,
    RootResumeDispatcher,
    OrchestrationOutboxDispatcher,
    ApprovalExpiryWorker,
    WorkerSupervisor,
)

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
    checkpointer = PostgresCheckpointer(settings.postgres_conn_string)
    graph_thread_lock = PostgresGraphThreadLock(settings.postgres_conn_string)
    session_factory = postgres.session
    sessions = PostgresSessionRepository(session_factory)
    session_memory_settings = PostgresSessionMemorySettingsRepository(session_factory)
    memory_project_spaces = PostgresMemoryProjectSpaceRepository(session_factory)
    messages = PostgresMessageRepository(session_factory)
    runs = PostgresRunRepository(session_factory)
    events = PostgresEventRepository(session_factory)
    approvals = PostgresApprovalRepository(session_factory)
    memory = PostgresMemoryRepository(session_factory)
    memory_jobs = PostgresMemoryJobRepository(session_factory)
    memory_operations = PostgresMemoryOperationsRepository(session_factory)
    memory_episodes = PostgresMemoryEpisodeRepository(session_factory)
    retrieval = PostgresRetrievalRepository(session_factory)
    evaluation_repository = PostgresEvaluationRepository(session_factory)
    evaluation_progress = EvaluationProgressHub()
    tools = PostgresToolConfigRepository(session_factory)
    mcp_repository = PostgresMCPServerRepository(session_factory)
    orchestration_repository = PostgresOrchestrationRepository(session_factory)
    orchestration_command_repository = PostgresOrchestrationCommandRepository(
        session_factory
    )
    orchestration_event_repository = PostgresOrchestrationEventRepository(
        session_factory,
        max_attempts=settings.worker_max_attempts,
    )
    worker_job_repository = PostgresWorkerJobRepository(session_factory)
    worker_tool_operation_repository = PostgresWorkerToolOperationRepository(
        session_factory
    )
    worker_execution_message_repository = PostgresWorkerExecutionMessageRepository(
        session_factory
    )
    root_resume_job_repository = PostgresRootResumeJobRepository(session_factory)
    orchestration_outbox_repository = PostgresOrchestrationOutboxRepository(
        session_factory
    )
    orchestration_command_handler = OrchestrationCommandHandler(
        orchestration_command_repository
    )
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
    file_vectors = PostgresFileVectorIndex(session_factory, embedding)
    memory_vectors = PostgresMemoryVectorIndex(session_factory, embedding)
    sandbox = DockerSandbox(
        SandboxOptions(
            docker_binary=settings.sandbox_docker_binary,
            default_network=settings.sandbox_default_network,
            max_output_bytes=settings.sandbox_max_output_bytes,
            memory_mb=settings.sandbox_memory_mb,
            cpu_limit=settings.sandbox_cpu_limit,
            pids_limit=settings.sandbox_pids_limit,
            mount_workspace=settings.sandbox_mount_workspace,
            read_only_root=settings.sandbox_read_only_root,
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
    memory_extractor = LLMFactExtractor(
        llm_provider,
        memory,
        operations=memory_operations,
        settings=session_memory_settings,
    )
    memory_resolver = LLMMemoryResolver(llm_provider, memory)
    memory_workflow = MemoryWriteWorkflow(
        memory,
        DefaultMemoryWriteTrigger(),
        memory_extractor,
        memory_resolver,
        memory_vectors,
        operations=memory_operations,
        settings=session_memory_settings,
        episode_summarizer=LLMMemoryEpisodeSummarizer(llm_provider),
        episodes=memory_episodes,
    )
    context_planner = ContextPlanner(
        max_files=settings.knowledge_context_max_files,
        limit_per_file=settings.knowledge_context_limit_per_file,
        max_items=settings.knowledge_context_max_items,
        max_tokens=settings.knowledge_context_max_tokens,
        recall_limit=settings.retrieval_candidate_k,
        rerank_limit=settings.retrieval_rerank_k,
        token_counter=llm_provider.count_text_tokens,
    )
    context_providers = {
        "memory": MemoryContextProvider(
            memory,
            memory_vectors,
            limit=settings.retrieval_context_k,
            timeout_seconds=settings.memory_retrieval_timeout_seconds,
            settings=session_memory_settings,
        ),
        "episode": MemoryEpisodeContextProvider(
            memory_episodes,
            limit=settings.retrieval_context_k,
            timeout_seconds=settings.memory_retrieval_timeout_seconds,
            settings=session_memory_settings,
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
    }
    context_service = ContextAcquisitionService(
        providers=context_providers,
        trace_repository=retrieval,
        event_publisher=events,
        memory_repository=memory,
        episode_repository=memory_episodes,
        token_counter=llm_provider.count_text_tokens,
    )

    request_preparation = RunRequestPreparationService(
        sessions,
        messages,
        attachments,
        knowledge,
        id_factory=ids,
        events=events,
    )

    builtin_tool_service = ToolService(
        tools,
        BuiltinToolExecutor(
            settings.files_path,
            max_output_bytes=settings.sandbox_max_output_bytes,
            sandbox=sandbox,
            sandbox_enabled=(settings.sandbox_enabled or settings.sandbox_required)
            and settings.exec_shell_enabled,
            sandbox_required=settings.sandbox_required,
            sandbox_image=settings.sandbox_shell_image,
            sandbox_max_timeout=settings.sandbox_max_timeout,
            sandbox_workspace_root=Path(settings.sandbox_workspace_root),
        ),
    )
    worker_result_storage = LocalFileStorage(
        settings.files_path.parent
        / f"{settings.files_path.name}-worker-results",
        settings.file_max_upload_bytes,
    )
    worker_result_store = WorkerResultStore(worker_result_storage)
    worker_tool_executor = WorkerToolExecutor(
        builtin_tool_service,
        result_store=worker_result_store,
        operation_ledger=worker_tool_operation_repository,
        events=events,
    )
    worker_agent = ConfiguredWorkerAgent(
        llm_provider,
        events,
        tool_service=builtin_tool_service,
        execution_messages=worker_execution_message_repository,
    )
    worker_executor = LangGraphWorkerExecutor(
        workflow_factory=lambda: WorkerWorkflow(
            execute=worker_agent,
            execute_tool=worker_tool_executor,
            execution_messages=worker_execution_message_repository,
            max_tool_rounds=settings.worker_max_tool_rounds,
            max_tool_calls_per_round=settings.worker_max_tool_calls_per_round,
        ),
        checkpointer=checkpointer,
    )
    approval_service = ApprovalService(approvals)
    worker_approval_service = WorkerApprovalService(
        worker_job_repository,
        timeout_seconds=settings.worker_approval_timeout_seconds,
    )
    worker_dispatcher = WorkerJobDispatcher(
        worker_job_repository,
        worker_executor,
        orchestration_event_repository,
        worker_id=f"worker-dispatcher:{ids()}",
        lease_seconds=settings.worker_job_lease_seconds,
        max_concurrency=settings.worker_dispatch_max_concurrency,
        approval_service=worker_approval_service,
        thread_lock=graph_thread_lock,
        poll_interval=settings.worker_job_poll_interval_seconds,
        id_factory=ids,
    )
    dependencies = RootWorkflowDependencies(
        context_acquisition=context_service,
        context_planner=context_planner,
        request_preparation=request_preparation,
        plan_loader=PostgresPlanLoader(orchestration_repository),
        memory_settings=session_memory_settings,
        agent=ConfiguredRootAgent(llm_provider, events),
        answer_generation=AnswerGenerationService(
            llm_provider,
            events,
            input_budget_tokens=settings.primary_llm.input_budget_tokens,
            options=AnswerStreamingOptions(
                flush_interval_ms=settings.answer_streaming.flush_interval_ms,
                max_chunk_chars=settings.answer_streaming.max_chunk_chars,
                first_text_timeout_ms=settings.answer_streaming.first_text_timeout_ms,
                idle_timeout_ms=settings.answer_streaming.idle_timeout_ms,
                total_timeout_ms=settings.answer_streaming.total_timeout_ms,
            ),
            prompt=(Path(__file__).resolve().parents[2] / "prompt" / "answer_generation.md").read_text(encoding="utf-8"),
        ),
        tool_service=builtin_tool_service,
        available_context_providers=tuple(context_providers),
        root_max_turns=settings.root_max_turns,
        context_prefetch_timeout_seconds=settings.context_prefetch_timeout_seconds,
        review_context_repository=orchestration_repository,
        orchestration_command_handler=orchestration_command_handler,
        event_publisher=events,
    )
    root_workflow = RootWorkflow(dependencies)
    root_graph = LangGraphRootGraph(
        workflow=root_workflow,
        dependencies=dependencies,
        checkpointer=checkpointer,
        thread_lock=graph_thread_lock,
    )

    root_run_executor = LangGraphRunExecutor(root_graph)
    providers: dict[str, Provider] = {
        "session_service_factory": lambda: SessionService(sessions, id_factory=ids),
        "session_memory_settings_factory": lambda: SessionMemorySettingsService(
            sessions, session_memory_settings
        ),
        "memory_project_factory": lambda: MemoryProjectSpaceService(
            memory_project_spaces,
            id_factory=ids,
        ),
        "message_query_factory": lambda: SessionMessageQueryService(sessions, messages),
        "run_query_factory": lambda: RunQueryService(sessions, runs, events),
        "run_command_factory": lambda: RunCommandService(
            sessions, runs, runs, id_factory=ids
        ),
        "event_stream_factory": lambda: EventStreamService(sessions, events),
        "tool_query_factory": lambda: ToolQueryService(tools),
        "tool_service_factory": lambda: builtin_tool_service,
        "approval_service_factory": lambda: approval_service,
        "mcp_service_factory": lambda: MCPService(mcp),
        "provider_query_factory": lambda: ProviderQueryService(settings_queries),
        "settings_query_factory": lambda: SettingsQueryService(settings_queries),
        "memory_service_factory": lambda: MemoryService(memory),
        "memory_episode_service_factory": lambda: MemoryEpisodeManagementService(
            memory_episodes
        ),
        "memory_job_service_factory": lambda: MemoryJobManagementService(memory_jobs),
        "retrieval_query_factory": lambda: RetrievalQueryService(retrieval),
        "evaluation_repository_factory": lambda: evaluation_repository,
        "evaluation_target_factory": lambda manifest: EmbeddingRetrievalTarget(
            manifest.corpus, embedding
        ),
        "evaluation_dataset_loader_factory": lambda path: load_dataset(path),
        "evaluation_event_sink_factory": lambda: EvaluationEventPublisher(
            events, fanout=evaluation_progress
        ),
        "evaluation_progress_hub_factory": lambda: evaluation_progress,
        "knowledge_query_factory": lambda: KnowledgeBaseQueryService(knowledge),
        "attachment_service_factory": lambda: AttachmentService(
            sessions, attachments, storage, adapters, jobs, id_factory=ids
        ),
        "knowledge_service_factory": lambda: KnowledgeBaseService(
            knowledge, documents, storage, adapters, jobs, id_factory=ids
        ),
        "orchestration_repository_factory": lambda: orchestration_repository,
        "orchestration_query_factory": lambda: OrchestrationQueryService(
            orchestration_repository
        ),
        "orchestration_command_handler_factory": lambda: orchestration_command_handler,
        "worker_task_event_handler_factory": lambda: worker_task_event_handler,
        "worker_job_repository_factory": lambda: worker_job_repository,
        "worker_job_dispatcher_factory": lambda: worker_dispatcher,
        "root_graph_factory": lambda: root_graph,
        "run_executor_factory": lambda: root_run_executor,
        "ingestion_service_factory": lambda: ingestion,
        "llm_provider": lambda: llm_provider,
        "embedding_provider": lambda: embedding,
        "sandbox": lambda: sandbox,
        "context_service_factory": lambda: context_service,
    }
    resources: list[object] = [postgres, checkpointer, root_graph, neo4j, embedding]
    resources.append(sandbox)
    if settings.workers_enabled:
        command_execution = RunExecutionService(
            runs,
            runs,
            root_run_executor,
            events,
            memory_jobs,
            runs,
        )
        command_worker = CommandConsumer(runs, command_execution)
        root_resume_dispatcher = RootResumeDispatcher(
            root_resume_job_repository,
            root_graph,
            command_execution,
            runs,
            worker_id=f"root-resume-dispatcher:{ids()}",
            lease_seconds=settings.worker_job_lease_seconds,
            poll_interval=settings.worker_job_poll_interval_seconds,
        )
        orchestration_outbox_dispatcher = OrchestrationOutboxDispatcher(
            orchestration_outbox_repository,
            events,
            worker_id=f"orchestration-outbox:{ids()}",
            lease_seconds=settings.worker_job_lease_seconds,
            poll_interval=settings.worker_job_poll_interval_seconds,
        )
        approval_expiry_worker = ApprovalExpiryWorker(
            approval_service,
            poll_interval=settings.worker_job_poll_interval_seconds,
        )
        document_worker = KnowledgeDocumentWorker(jobs, ingestion)
        memory_worker = MemoryWorker(memory_jobs, memory_workflow)
        resources.append(
            WorkerSupervisor(
                (
                    command_worker,
                    document_worker,
                    memory_worker,
                    worker_dispatcher,
                    root_resume_dispatcher,
                    orchestration_outbox_dispatcher,
                    approval_expiry_worker,
                )
            )
        )
    return build_dependencies(factories=providers, resources=resources)


__all__ = ["build_postgres_dependencies"]
