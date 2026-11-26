"""API 与 Run Worker 共享的组合根：集中装配核心仓储、文件/技能服务与查询用例。

应用层只消费领域端口；具体仓储、模型资源与外部客户端的选择全部收敛在本模块，
保证 API 与 Run Worker 使用同一套强类型配置和依赖装配规则。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.access import SqlAlchemyAccessControlRepository
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.file import SqlAlchemySessionFileRepository
from adapter.knowledge.repository import KnowledgeRepository
from adapter.knowledge.storage import MinioStorageConfig
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.mcp.repository import SqlAlchemyMcpRepository
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.preference import SqlAlchemyPreferenceRepository
from adapter.project.repository import SqlAlchemyProjectRepository
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session import SqlAlchemySessionRepository
from adapter.skill import SqlAlchemySkillRepository
from adapter.subagent import SqlAlchemySubagentRepository
from adapter.system_prompt import SqlAlchemySystemPromptRepository
from app.access import AccessPolicyService
from app.file.service import SessionFileService
from app.knowledge.service import KnowledgeService
from app.preference import PreferenceService
from app.project.knowledge import ProjectKnowledgeRepository, ProjectKnowledgeService
from app.project.service import ProjectService
from app.skill import DependencyChecker, SkillPackageCache, SkillService
from app.skill.execution_service import SkillExecutionService
from app.tool.runtime import RunToolRuntimeService
from bootstrap.config import KnowledgeSettings, McpSettings
from bootstrap.knowledge import (
    build_knowledge_models,
    build_knowledge_original_store,
)
from bootstrap.lifecycle import close_resources_lifo
from domain.auth import CurrentSessionReader
from domain.file.ports import FileObjectStore
from domain.knowledge.ports import (
    KnowledgeRepositoryPort,
    OriginalStore,
)
from domain.mcp import McpSecretError, McpToolExecutor, McpToolRuntimeReader
from domain.session import SessionRepository
from domain.subagent import SubagentRepository
from infrastructure.embedding import OpenAICompatibleEmbeddingConfig
from infrastructure.file import LocalSessionDocumentExtractor, LocalVisionImageNormalizer
from infrastructure.mcp.execution import McpToolCallClient
from infrastructure.security.mcp_secrets import McpSecretCipher
from infrastructure.storage.minio_client import (
    SHARED_EXECUTOR_MAX_WORKERS,
    SHARED_HTTP_POOL_MAXSIZE,
    MinioObjectClient,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CorePersistenceBundle:
    """API 与 Run Worker 共享的核心数据库仓储与基础服务集合。"""

    access_policy: AccessPolicyService
    auth_repository: SqlAlchemyAuthRepository
    project_repository: SqlAlchemyProjectRepository
    project_service: ProjectService
    model_profile_repository: SqlAlchemyModelProfileRepository
    message_repository: SqlAlchemyMessageRepository
    session_repository: SqlAlchemySessionRepository
    system_prompt_repository: SqlAlchemySystemPromptRepository
    preference_service: PreferenceService
    mcp_repository: SqlAlchemyMcpRepository | None
    run_repository: SqlAlchemyRunRepository
    skill_repository: SqlAlchemySkillRepository
    subagent_repository: SqlAlchemySubagentRepository


def build_core_persistence(
    sessions: async_sessionmaker[AsyncSession],
    *,
    secret_cipher: McpSecretCipher | None,
    include_mcp: bool = True,
    project_service: ProjectService | None = None,
    preference_service: PreferenceService | None = None,
    auth_repository_factory: Callable[[async_sessionmaker[AsyncSession]], Any] = (
        SqlAlchemyAuthRepository
    ),
    mcp_repository_factory: Callable[
        [async_sessionmaker[AsyncSession], McpSecretCipher], Any
    ] = SqlAlchemyMcpRepository,
    model_profile_repository_factory: Callable[
        [async_sessionmaker[AsyncSession], McpSecretCipher | None], Any
    ] = SqlAlchemyModelProfileRepository,
    message_repository_factory: Callable[[async_sessionmaker[AsyncSession]], Any] = (
        SqlAlchemyMessageRepository
    ),
    run_repository_factory: Callable[[async_sessionmaker[AsyncSession], Any], Any] = (
        SqlAlchemyRunRepository
    ),
) -> CorePersistenceBundle:
    """统一构造 API 与 Run Worker 共享的 13 个核心仓储与基础服务，消除跨进程重复装配。"""
    access_policy = AccessPolicyService(SqlAlchemyAccessControlRepository(sessions))
    auth_repository = auth_repository_factory(sessions)
    project_repository = SqlAlchemyProjectRepository(sessions)
    resolved_project_service = (
        project_service if project_service is not None else ProjectService(project_repository)
    )
    model_profile_repository = model_profile_repository_factory(sessions, secret_cipher)
    message_repository = message_repository_factory(sessions)
    session_repository = SqlAlchemySessionRepository(sessions)
    system_prompt_repository = SqlAlchemySystemPromptRepository(sessions)
    resolved_preference_service = (
        preference_service
        if preference_service is not None
        else PreferenceService(SqlAlchemyPreferenceRepository(sessions))
    )
    mcp_repository: SqlAlchemyMcpRepository | None = None
    if include_mcp:
        if secret_cipher is None:
            raise McpSecretError("mcp_secret_key_missing")
        mcp_repository = mcp_repository_factory(sessions, secret_cipher)
    run_repository = run_repository_factory(sessions, message_repository)
    skill_repository = SqlAlchemySkillRepository(sessions)
    subagent_repository = SqlAlchemySubagentRepository(sessions)
    return CorePersistenceBundle(
        access_policy=access_policy,
        auth_repository=auth_repository,
        project_repository=project_repository,
        project_service=resolved_project_service,
        model_profile_repository=model_profile_repository,
        message_repository=message_repository,
        session_repository=session_repository,
        system_prompt_repository=system_prompt_repository,
        preference_service=resolved_preference_service,
        mcp_repository=mcp_repository,
        run_repository=run_repository,
        skill_repository=skill_repository,
        subagent_repository=subagent_repository,
    )


def build_session_file_service(
    sessions: async_sessionmaker[AsyncSession],
    object_store: FileObjectStore,
) -> SessionFileService:
    """统一构造会话文件服务（含本地文档抽取与视觉图像规范化器）。"""
    return SessionFileService(
        SqlAlchemySessionFileRepository(sessions),
        object_store,
        document_extractor=LocalSessionDocumentExtractor(),
        image_normalizer=LocalVisionImageNormalizer(),
    )


def build_skill_service(
    *,
    skill_repository: SqlAlchemySkillRepository,
    object_store: FileObjectStore | None,
    auth_repository: CurrentSessionReader,
    access_policy: AccessPolicyService | None,
    python_executable: Path,
    package_cache: SkillPackageCache | None = None,
    skill_service_factory: Callable[..., SkillService] = SkillService,
) -> SkillService:
    """统一构造技能服务；未启用对象存储时纯 Markdown 技能仍可正常使用。"""
    return skill_service_factory(
        skill_repository,
        object_store,
        auth_repository,
        access_policy=access_policy,
        dependency_checker=DependencyChecker(python_executable=python_executable),
        package_cache=package_cache,
    )


def build_shared_storage_client(
    *,
    storage_config: MinioStorageConfig | None = None,
) -> MinioObjectClient:
    """构造进程内文件/技能与知识库共用的单实例 MinIO 底层引擎（单连接池 + 单线程池）。"""
    # 与知识库启动前置校验保持一致，非法配置抛出 OriginalStorageConfigurationError。
    config = storage_config if storage_config is not None else MinioStorageConfig.from_environment()
    return MinioObjectClient(
        config,
        max_workers=SHARED_EXECUTOR_MAX_WORKERS,
        http_pool_maxsize=SHARED_HTTP_POOL_MAXSIZE,
        http_pool_block=False,
    )


@dataclass(slots=True)
class QueryServiceBundle:
    """知识库查询服务及其进程级无状态资源。"""

    knowledge: KnowledgeService | None
    project_knowledge: ProjectKnowledgeService | None
    # 对外只暴露领域端口，应用层不需要知道具体仓储实现。
    knowledge_repository: KnowledgeRepositoryPort | None
    knowledge_original_store: OriginalStore | None
    knowledge_enabled: bool
    _startables: tuple[Any, ...]
    _closables: tuple[Any, ...]

    async def start(self) -> None:
        """在首个请求前预热已装配的外部存储、无状态客户端与中文分词词典。"""
        for resource in self._startables:
            start = getattr(resource, "start", None)
            if callable(start):
                await start()
                continue
            warmup = getattr(resource, "warmup", None)
            if callable(warmup):
                await asyncio.to_thread(warmup)

    async def aclose(self) -> None:
        """经 AsyncExitStack 按逆序独立释放所有进程级查询资源。"""

        def _make_cleanup(resource: Any) -> Callable[[], Any]:
            def _close() -> Any:
                aclose_fn = getattr(resource, "aclose", None)
                if aclose_fn is not None:
                    return aclose_fn()
                close_fn = getattr(resource, "close", None)
                if close_fn is not None:
                    return close_fn()
                return None

            return _close

        cleanups = [
            ("query_service_cleanup_failed", _make_cleanup(resource))
            for resource in self._closables
        ]
        await close_resources_lifo(
            cleanups,
            log=logger,
            reraise=True,
            log_level=logging.WARNING,
        )


def build_query_services(
    sessions: async_sessionmaker[AsyncSession],
    *,
    projects: ProjectService,
    knowledge_service: KnowledgeService | None = None,
    access_policy: AccessPolicyService | None = None,
    object_client: MinioObjectClient | None = None,
    knowledge_enabled: bool | None = None,
    semantic_min_score: float | None = None,
    embedding_config: OpenAICompatibleEmbeddingConfig | None = None,
    storage_config: MinioStorageConfig | None = None,
) -> QueryServiceBundle:
    """按统一强类型配置为 API 与 Run Worker 装配知识库查询服务与实时权限引擎。"""
    knowledge_settings: KnowledgeSettings | None = None
    if knowledge_enabled is None:
        knowledge_settings = KnowledgeSettings.from_environment()
        resolved_enabled = knowledge_service is not None or knowledge_settings.enabled
    else:
        resolved_enabled = knowledge_service is not None or knowledge_enabled
    startables: list[Any] = []
    closables: list[Any] = []
    knowledge = knowledge_service
    resolved_access_policy = (
        access_policy
        if access_policy is not None
        else AccessPolicyService(SqlAlchemyAccessControlRepository(sessions))
    )
    # 组合根是唯一允许构造具体适配器的地方；bundle 只暴露领域端口。
    knowledge_repository: KnowledgeRepositoryPort | None = None
    # 项目知识关联需要额外的仓储能力，构造期单独持有具体实现，不进入公开契约。
    project_knowledge_repository: ProjectKnowledgeRepository | None = None
    knowledge_originals: OriginalStore | None = None
    if resolved_enabled and knowledge is None:
        # 先校验并构造无状态模型与检索阈值，再构造原件存储包装层，避免配置错误中途遗留资源。
        models = build_knowledge_models(embedding_config=embedding_config)
        if semantic_min_score is not None:
            resolved_min_score = semantic_min_score
        else:
            if knowledge_settings is None:
                knowledge_settings = KnowledgeSettings.from_environment()
            resolved_min_score = knowledge_settings.semantic_min_score
        minio_originals = build_knowledge_original_store(
            object_client=object_client,
            storage_config=storage_config,
        )
        knowledge_originals = minio_originals
        concrete_repository = KnowledgeRepository(sessions)
        knowledge_repository = concrete_repository
        project_knowledge_repository = concrete_repository
        knowledge = KnowledgeService(
            concrete_repository,
            models.tokenizer,
            models.embedding,
            knowledge_originals,
            models.counter,
            access_policy=resolved_access_policy,
            profile_id=models.profile_id,
            semantic_min_score=resolved_min_score,
        )
        # 启动顺序沿用存储 → 计数 → 远程向量客户端 → 中文分词词典预热，关闭时按相反依赖顺序释放。
        startables.extend((minio_originals, models.counter, models.embedding, models.tokenizer))
        closables.extend((minio_originals, models.embedding, models.counter))
    elif knowledge is not None and knowledge_repository is None:
        concrete_repository = KnowledgeRepository(sessions)
        knowledge_repository = concrete_repository
        project_knowledge_repository = concrete_repository
    project_knowledge = (
        None
        if not resolved_enabled or knowledge is None or project_knowledge_repository is None
        else ProjectKnowledgeService(
            projects,
            project_knowledge_repository,
            access_policy=resolved_access_policy,
        )
    )
    return QueryServiceBundle(
        knowledge=knowledge,
        project_knowledge=project_knowledge,
        knowledge_repository=knowledge_repository,
        knowledge_original_store=knowledge_originals,
        knowledge_enabled=resolved_enabled,
        _startables=tuple(startables),
        _closables=tuple(closables),
    )


def build_mcp_executor(
    *,
    cleanup_timeout_seconds: float = 10.0,
) -> McpToolCallClient:
    """Create the single-call MCP executor with the deployment transport limits."""
    mcp_settings = McpSettings.from_environment()
    return McpToolCallClient(
        allowed_private_hosts=mcp_settings.allowed_private_hosts,
        stdio_base_environment=mcp_settings.stdio_base_environment(),
        stdio_allowed_environment_names=mcp_settings.stdio_base_env_names,
        cleanup_timeout_seconds=cleanup_timeout_seconds,
    )


def build_run_tool_runtime(
    *,
    auth: CurrentSessionReader,
    sessions: SessionRepository,
    mcp_catalog: McpToolRuntimeReader,
    mcp_executor: McpToolExecutor,
    query_services: QueryServiceBundle,
    preferences: PreferenceService | None = None,
    skills: SkillService | None = None,
    session_files: SessionFileService | None = None,
    skill_execution: SkillExecutionService | None = None,
    subagents: SubagentRepository | None = None,
    access_policy: AccessPolicyService | None = None,
) -> RunToolRuntimeService:
    """绑定查询服务、轻量偏好服务、统一权限引擎与 MCP 端口到 Run 工具运行时。"""
    return RunToolRuntimeService(
        auth=auth,
        sessions=sessions,
        mcp_catalog=mcp_catalog,
        mcp_executor=mcp_executor,
        knowledge=query_services.knowledge,
        project_knowledge=query_services.project_knowledge,
        preferences=preferences,
        skills=skills,
        session_files=session_files,
        skill_execution=skill_execution,
        subagents=subagents,
        access_policy=access_policy,
    )


__all__ = [
    "CorePersistenceBundle",
    "QueryServiceBundle",
    "build_core_persistence",
    "build_mcp_executor",
    "build_query_services",
    "build_run_tool_runtime",
    "build_session_file_service",
    "build_shared_storage_client",
    "build_skill_service",
]
