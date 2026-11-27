"""Run Worker 进程的组合根：集中装配仓储、LLM、Redis 与 Worker。"""

from __future__ import annotations

from dataclasses import dataclass

from adapter.file import MinioFileObjectStore
from adapter.llm import OpenAIChatCompletionsAdapter
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.run.redis_transport import RedisRunTransport
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.skill.execution import PythonScriptExecutor
from app.run import RunExecutor, RunWorker, worker_config_from_environment
from app.skill import SkillPackageCache, SkillService
from app.skill.execution_service import SkillExecutionService
from bootstrap.config import (
    DatabaseSettings,
    FileSkillSettings,
    KnowledgeSettings,
)
from bootstrap.database import build_database
from bootstrap.knowledge import (
    knowledge_embedding_config,
    validate_knowledge_storage_configuration,
)
from bootstrap.redis import build_run_transport
from bootstrap.tool import (
    QueryServiceBundle,
    build_core_persistence,
    build_mcp_executor,
    build_query_services,
    build_run_tool_runtime,
    build_session_file_service,
    build_shared_storage_client,
    build_skill_service,
)
from domain.file import FileObjectStore
from infrastructure.database import Database
from infrastructure.mcp.execution import McpToolCallClient
from infrastructure.security.secrets import SecretCipher
from infrastructure.storage.minio_client import MinioObjectClient


@dataclass(slots=True)
class RunWorkerBundle:
    """Run Worker 构造过程中已创建、需要由入口清理的资源。"""

    database: Database | None = None
    provider: OpenAIChatCompletionsAdapter | None = None
    run_transport: RedisRunTransport | None = None
    mcp_executor: McpToolCallClient | None = None
    storage_client: MinioObjectClient | None = None
    query_services: QueryServiceBundle | None = None
    file_object_store: FileObjectStore | None = None
    skill_package_cache: SkillPackageCache | None = None
    skill_execution: SkillExecutionService | None = None
    worker: RunWorker | None = None


def build_run_worker_dependencies(
    bundle: RunWorkerBundle,
    *,
    database_url: str | None = None,
) -> None:
    """按依赖顺序组装 Run Worker，并把资源逐项写入 bundle。

    query_services 的资源启动是异步的，由入口在调用本函数后执行。构造中途
    失败时，入口 finally 只清理 bundle 中已填充的字段，因此这里每完成一个
    资源就立即写回 bundle。
    """
    # 数据库地址优先使用入参，其次读取强类型环境变量；缺失时先于其他配置解析直接拒绝。
    configured_url = database_url or DatabaseSettings.url_from_environment()
    if not configured_url:
        raise RuntimeError("run_worker_database_required")
    # 在创建数据库连接池与共享对象存储客户端之前，前置校验全部必填配置（Fail-Fast），
    # 防止配置缺失或非法时遗留连接池或线程池资源。
    config = worker_config_from_environment()
    # MCP 凭据解封与 API 使用同一密钥；缺失或损坏时拒绝启动，不能静默禁用工具。
    secret_cipher = SecretCipher.from_environment()
    file_settings = FileSkillSettings.from_environment()
    knowledge_settings = KnowledgeSettings.from_environment()
    storage_config = validate_knowledge_storage_configuration()
    embedding_config = knowledge_embedding_config()

    database = build_database(configured_url)
    bundle.database = database

    core = build_core_persistence(
        database.session_factory,
        secret_cipher=secret_cipher,
        include_mcp=True,
        # 显式传入模块级仓储类，保持测试对 bootstrap.run_worker 符号的 patch 兼容。
        model_profile_repository_factory=SqlAlchemyModelProfileRepository,
        message_repository_factory=SqlAlchemyMessageRepository,
        run_repository_factory=SqlAlchemyRunRepository,
    )
    assert core.mcp_repository is not None
    mcp_executor = build_mcp_executor(cleanup_timeout_seconds=config.tool_cleanup_seconds)
    # 持有 MCP 执行器以在 Worker 退出时释放池化 HTTP transport 与清空工具清单缓存。
    bundle.mcp_executor = mcp_executor

    # Run Worker 无条件装配知识库与文件/技能能力，全程共享单套底层 MinIO 客户端，
    # 创建后立即登记到 bundle，确保中途失败或退出时由入口统一逆序释放。
    shared_storage_client = build_shared_storage_client(storage_config=storage_config)
    bundle.storage_client = shared_storage_client
    query_services = build_query_services(
        database.session_factory,
        projects=core.project_service,
        access_policy=core.access_policy,
        object_client=shared_storage_client,
        semantic_min_score=knowledge_settings.semantic_min_score,
        embedding_config=embedding_config,
        storage_config=storage_config,
    )
    bundle.query_services = query_services

    object_store: FileObjectStore = MinioFileObjectStore(object_client=shared_storage_client)
    bundle.file_object_store = object_store
    # 共享单个有界内存 ZIP 字节 LRU 缓存，由 Worker 退出流程统一 aclose() 释放。
    package_cache = SkillPackageCache()
    bundle.skill_package_cache = package_cache

    skills = build_skill_service(
        skill_repository=core.skill_repository,
        object_store=object_store,
        auth_repository=core.auth_repository,
        access_policy=core.access_policy,
        python_executable=file_settings.python_executable,
        package_cache=package_cache,
        skill_service_factory=SkillService,
    )
    session_files = build_session_file_service(database.session_factory, object_store)
    skill_execution = SkillExecutionService(
        skills=skills,
        files=session_files,
        repository=core.skill_repository,
        object_store=object_store,
        runner=PythonScriptExecutor(file_settings.python_executable),
        temp_root=file_settings.temp_root,
        package_cache=package_cache,
    )
    bundle.skill_execution = skill_execution

    tool_runtime = build_run_tool_runtime(
        auth=core.auth_repository,
        sessions=core.session_repository,
        mcp_catalog=core.mcp_repository,
        mcp_executor=mcp_executor,
        query_services=query_services,
        preferences=core.preference_service,
        skills=skills,
        session_files=session_files,
        skill_execution=skill_execution,
        subagents=core.subagent_repository,
        access_policy=core.access_policy,
    )
    provider = OpenAIChatCompletionsAdapter()
    bundle.provider = provider
    # Redis 未配置时保持为空，后续以 NullRunProgressPublisher 降级运行，
    # 仅失去实时推送，不影响 Run 的持久化与状态流转。
    run_transport = build_run_transport()
    bundle.run_transport = run_transport
    # Executor 负责执行单个 Run；Worker 负责领取、续约并驱动执行。
    executor = RunExecutor(
        core.run_repository,
        core.model_profile_repository,
        provider,
        system_prompts=core.system_prompt_repository,
        publisher=run_transport,
        config=config,
        tool_runtime=tool_runtime,
        preferences=core.preference_service,
        projects=core.project_repository,
        sessions=core.session_repository,
        subagents=core.subagent_repository,
        access_policy=core.access_policy,
    )
    bundle.worker = RunWorker(core.run_repository, executor, config, publisher=run_transport)


__all__ = ["RunWorkerBundle", "build_run_worker_dependencies"]
