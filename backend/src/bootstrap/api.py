"""API 进程的组合根：集中装配数据库、Redis、LLM 与查询用例。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.file import MinioFileObjectStore, MinioFileStorageConfig
from adapter.knowledge.storage import MinioStorageConfig
from adapter.mcp.repository import SqlAlchemyMcpRepository
from adapter.org import SqlAlchemyOrgRepository
from adapter.run.redis_transport import RedisRunTransport
from adapter.subagent import SqlAlchemySubagentModelChecker
from app.auth.service import AuthService
from app.file import SessionFileService
from app.knowledge.service import KnowledgeService
from app.llm import ModelProfileService
from app.message.service import MessageService
from app.org import FeishuOrgSyncService, OrgService, seed_mock_organization
from app.preference import PreferenceService
from app.project.service import ProjectService
from app.run.service import RunService
from app.run.stream import RunStreamService
from app.session.service import SessionService
from app.skill import SkillService
from app.subagent import DefaultSubagentToolCatalog, SubagentService
from app.system_prompt import SystemPromptService
from app.tool import ToolManager
from bootstrap.config import (
    AuthSettings,
    DatabaseSettings,
    FileSkillSettings,
    KnowledgeSettings,
    McpSettings,
)
from bootstrap.database import build_database
from bootstrap.knowledge import (
    validate_knowledge_embedding_configuration,
    validate_knowledge_storage_configuration,
)
from bootstrap.lifecycle import close_resources_lifo
from bootstrap.redis import (
    build_auth_rate_limiters,
    build_oauth_state_store,
    build_redis_client,
    build_run_transport,
    is_production_environment,
    validate_auth_rate_limit_configuration,
    validate_feishu_state_store_configuration,
)
from bootstrap.tool import (
    QueryServiceBundle,
    build_core_persistence,
    build_query_services,
    build_session_file_service,
    build_shared_storage_client,
    build_skill_service,
)
from domain.file.ports import FileObjectStore
from domain.knowledge.ports import (
    DocumentFileValidator,
    KnowledgeRepositoryPort,
    OriginalStore,
)
from domain.mcp import McpSecretError
from domain.org import OrgRepository
from infrastructure.database import Database
from infrastructure.embedding import OpenAICompatibleEmbeddingConfig
from infrastructure.feishu import FeishuClient, FeishuConfig
from infrastructure.knowledge.parsers import LocalDocumentParser
from infrastructure.mcp.discovery import McpDiscoveryClient
from infrastructure.security.mcp_secrets import McpSecretCipher
from infrastructure.security.password import Argon2PasswordHasher
from infrastructure.storage.minio_client import MinioObjectClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ApiDependencies:
    """API 进程持有并负责清理的进程级资源。"""

    database: Database | None
    run_transport: RedisRunTransport | None
    query_services: QueryServiceBundle | None
    project_service: ProjectService | None
    message_service: MessageService | None
    session_service: SessionService | None
    run_service: RunService | None
    auth_service: AuthService | None
    tool_manager: ToolManager | None
    model_profile_service: ModelProfileService | None
    system_prompt_service: SystemPromptService | None
    preference_service: PreferenceService | None
    knowledge_service: KnowledgeService | None
    knowledge_repository: KnowledgeRepositoryPort | None
    knowledge_originals: OriginalStore | None
    run_stream_service: RunStreamService | None
    document_validator: DocumentFileValidator | None
    file_object_store: FileObjectStore | None
    skill_service: SkillService | None
    session_file_service: SessionFileService | None
    subagent_service: SubagentService | None = None
    org_service: OrgService | None = None
    org_repository: OrgRepository | None = None
    feishu_client: FeishuClient | None = None
    auth_mock_enabled: bool = False
    redis_client: Redis | None = None
    storage_client: MinioObjectClient | None = None

    async def start(self) -> None:
        """在首个请求前启动共享查询资源，并在非生产 Mock 模式下幂等预置组织与账号种子。"""
        if self.auth_mock_enabled and self.org_repository is not None:
            await seed_mock_organization(self.org_repository)
        if self.file_object_store is not None:
            await self.file_object_store.start()
        if self.query_services is not None:
            await self.query_services.start()

    async def aclose(self) -> None:
        """经 AsyncExitStack 按依赖逆序释放资源；单项失败仅记录日志不阻断后续清理。"""

        async def _close_redis(client: Redis) -> None:
            async with asyncio.timeout(1.0):
                await client.aclose()

        # 按创建顺序登记，AsyncExitStack 退出时按逆序执行：
        # feishu -> run_transport -> redis -> query_services -> file_store -> storage_client -> db。
        cleanups: list[tuple[str, Callable[[], Any]]] = []
        if self.database is not None:
            database = self.database
            cleanups.append(("database_shutdown_failed", lambda: database.dispose()))
        if self.storage_client is not None:
            storage_client = self.storage_client
            cleanups.append(("shared_storage_shutdown_failed", lambda: storage_client.aclose()))
        if self.file_object_store is not None:
            file_store = self.file_object_store
            cleanups.append(("file_storage_shutdown_failed", lambda: file_store.aclose()))
        if self.query_services is not None:
            query_services = self.query_services
            cleanups.append(("query_service_shutdown_failed", lambda: query_services.aclose()))
        if self.redis_client is not None:
            redis_client = self.redis_client
            cleanups.append(("redis_client_shutdown_failed", lambda: _close_redis(redis_client)))
        if self.run_transport is not None:
            run_transport = self.run_transport
            cleanups.append(("run_redis_shutdown_failed", lambda: run_transport.aclose()))
        if self.feishu_client is not None:
            feishu_client = self.feishu_client
            cleanups.append(("feishu_client_shutdown_failed", lambda: feishu_client.aclose()))
        await close_resources_lifo(
            cleanups,
            log=logger,
            reraise=False,
            catch_base_exception=False,
        )


def build_api_dependencies(
    project_service: ProjectService | None = None,
    message_service: MessageService | None = None,
    session_service: SessionService | None = None,
    run_service: RunService | None = None,
    auth_service: AuthService | None = None,
    *,
    tool_manager: ToolManager | None = None,
    model_profile_service: ModelProfileService | None = None,
    system_prompt_service: SystemPromptService | None = None,
    preference_service: PreferenceService | None = None,
    knowledge_service: KnowledgeService | None = None,
    skill_service: SkillService | None = None,
    session_file_service: SessionFileService | None = None,
    subagent_service: SubagentService | None = None,
    org_service: OrgService | None = None,
) -> ApiDependencies:
    """按强类型环境配置与注入参数组装 API 进程依赖。

    注入的用例保持调用方所有权，不在此关闭；只有本函数自行创建的资源
    进入返回值的清理生命周期。
    """
    auth_settings = AuthSettings.from_environment()
    # 强制生产安全护栏：生产环境严禁开启本地 Mock 快捷登录，在分配任何连接池前立即阻断启动。
    if is_production_environment() and auth_settings.mock_enabled:
        raise RuntimeError("auth_mock_forbidden_in_production")

    feishu_config: FeishuConfig | None = None
    if auth_settings.feishu_enabled:
        # 生产环境开启飞书 SSO 必须提供 Redis 一次性 state 存储，并校验凭据与 HTTPS 配置。
        validate_feishu_state_store_configuration()
        feishu_config = FeishuConfig.from_environment(
            is_production=is_production_environment(),
        )

    database_url = DatabaseSettings.url_from_environment()
    knowledge_settings = KnowledgeSettings.from_environment()
    file_settings = FileSkillSettings.from_environment()

    project_use_case = project_service
    message_use_case = message_service
    session_use_case = session_service
    run_use_case = run_service
    auth_use_case = auth_service
    org_use_case = org_service
    mcp_use_case = tool_manager
    model_profile_use_case = model_profile_service
    system_prompt_use_case = system_prompt_service
    subagent_use_case = subagent_service
    preference_use_case = preference_service
    knowledge_use_case = knowledge_service
    skill_use_case = skill_service
    session_file_use_case = session_file_service

    has_database = bool(database_url)
    # 知识库与文件存储作为后端核心能力默认装配；无 ATHENA_DATABASE_URL 时保持轻量模式，
    # 供纯 /health 探活及全服务注入单测使用，不要求数据库、对象存储或 Embedding。
    knowledge_owned = has_database and knowledge_use_case is None
    needs_file_storage = has_database and (skill_use_case is None or session_file_use_case is None)

    if auth_use_case is None:
        # 生产环境必须在启动初期拒绝缺失 Redis 限流的配置，避免分配数据库连接池后再失败。
        validate_auth_rate_limit_configuration()

    # 在创建数据库连接池之前执行前置 Fail-Fast 校验，防止对象存储或 Embedding 配置非法时
    # 已建库导致连接池等资源遗留；同时仅解析一次并在后续装配中复用。
    knowledge_storage_config: MinioStorageConfig | None = None
    knowledge_embedding_config: OpenAICompatibleEmbeddingConfig | None = None
    file_storage_config: MinioFileStorageConfig | None = None
    if knowledge_owned:
        knowledge_storage_config = validate_knowledge_storage_configuration()
        knowledge_embedding_config = validate_knowledge_embedding_configuration()
    elif needs_file_storage:
        file_storage_config = MinioFileStorageConfig.from_environment()

    # LLM 配置由管理员持久化；部署环境只提供加密主密钥，不提供模型凭据。
    secret_cipher: McpSecretCipher | None = None
    mcp_discovery: McpDiscoveryClient | None = None
    needs_mcp_repo = mcp_use_case is None or subagent_use_case is None
    if database_url and needs_mcp_repo:
        # 在创建数据库连接池前验证 MCP 密钥，防止失败组装遗留资源。
        secret_cipher = McpSecretCipher.from_environment()
        if mcp_use_case is None:
            mcp_settings = McpSettings.from_environment()
            mcp_discovery = McpDiscoveryClient(
                stdio_base_environment=mcp_settings.stdio_base_environment(),
                stdio_allowed_environment_names=mcp_settings.stdio_base_env_names,
            )
    if database_url and model_profile_use_case is None and secret_cipher is None:
        # LLM 加密密钥未配置时仍允许非 MCP 功能启动；模型调用会安全地保持不可用。
        try:
            secret_cipher = McpSecretCipher.from_environment()
        except McpSecretError:
            secret_cipher = None

    database: Database | None = None
    org_repository: OrgRepository | None = None
    feishu_client: FeishuClient | None = None
    knowledge_repository: KnowledgeRepositoryPort | None = None
    knowledge_originals: OriginalStore | None = None
    query_services: QueryServiceBundle | None = None
    shared_storage_client: MinioObjectClient | None = None
    file_object_store: FileObjectStore | None = None
    run_stream_service: RunStreamService | None = None
    run_transport: RedisRunTransport | None = None
    redis_client: Redis | None = None

    # 完整覆盖所有依赖数据库装配的核心用例（含知识库、Skill 与 SessionFile）。
    needs_database = bool(
        database_url
        and (
            auth_use_case is None
            or org_use_case is None
            or project_use_case is None
            or message_use_case is None
            or session_use_case is None
            or run_use_case is None
            or mcp_use_case is None
            or knowledge_use_case is None
            or system_prompt_use_case is None
            or preference_use_case is None
            or subagent_use_case is None
            or skill_use_case is None
            or session_file_use_case is None
        )
    )
    if needs_database:
        assert database_url is not None
        database = build_database(database_url)
        core = build_core_persistence(
            database.session_factory,
            secret_cipher=secret_cipher,
            include_mcp=needs_mcp_repo,
            project_service=project_use_case,
            preference_service=preference_use_case,
            # 显式传入模块级仓储类，保持测试对 bootstrap.api 符号的 monkeypatch 兼容。
            auth_repository_factory=SqlAlchemyAuthRepository,
            mcp_repository_factory=SqlAlchemyMcpRepository,
        )
        project_use_case = core.project_service
        preference_use_case = core.preference_service
        if model_profile_use_case is None:
            model_profile_use_case = ModelProfileService(
                core.model_profile_repository,
                core.auth_repository,
            )
        org_repository = SqlAlchemyOrgRepository(database.session_factory)
        if feishu_config is not None:
            feishu_client = FeishuClient(feishu_config)
        feishu_sync_service = (
            FeishuOrgSyncService(feishu_client, org_repository)
            if feishu_client is not None
            else None
        )
        if org_use_case is None:
            org_use_case = OrgService(
                org_repository,
                core.auth_repository,
                feishu_sync_service=feishu_sync_service,
            )
        if system_prompt_use_case is None:
            system_prompt_use_case = SystemPromptService(
                core.system_prompt_repository,
                core.auth_repository,
            )
        if session_use_case is None:
            session_use_case = SessionService(core.session_repository)
        if message_use_case is None:
            message_use_case = MessageService(core.message_repository)
        if mcp_use_case is None:
            if mcp_discovery is None or core.mcp_repository is None:
                raise McpSecretError("mcp_secret_key_missing")
            mcp_use_case = ToolManager(
                core.mcp_repository,
                mcp_discovery,
                core.auth_repository,
                access_policy=core.access_policy,
            )
        if subagent_use_case is None:
            subagent_use_case = SubagentService(
                core.subagent_repository,
                core.auth_repository,
                access_policy=core.access_policy,
                model_checker=SqlAlchemySubagentModelChecker(database.session_factory),
                tool_catalog=DefaultSubagentToolCatalog(core.mcp_repository),
            )
        # 同进程同时自建知识库与文件/技能对象存储时，共享单个底层 MinIO 客户端、连接池与线程池；
        # 仅单侧自建时各自独占底层客户端，不创建孤儿共享客户端。
        if knowledge_owned and needs_file_storage:
            shared_storage_client = build_shared_storage_client(
                storage_config=knowledge_storage_config,
            )
        query_services = build_query_services(
            database.session_factory,
            projects=project_use_case,
            knowledge_service=knowledge_use_case,
            access_policy=core.access_policy,
            object_client=shared_storage_client,
            semantic_min_score=knowledge_settings.semantic_min_score,
            embedding_config=knowledge_embedding_config,
            storage_config=knowledge_storage_config,
        )
        knowledge_use_case = query_services.knowledge
        knowledge_repository = query_services.knowledge_repository
        knowledge_originals = query_services.knowledge_original_store
        if run_use_case is None:
            run_use_case = RunService(core.run_repository)
        # 在数据库与查询服务均构建成功后再建立共享 Redis 客户端，供认证分布式限流与 Run SSE 共用，
        # 所有权统一归 ApiDependencies.aclose() 释放。
        redis_client = build_redis_client()
        if auth_use_case is None:
            oauth_state_store = (
                build_oauth_state_store(redis_client) if feishu_client is not None else None
            )
            if redis_client is not None:
                rate_limiters = build_auth_rate_limiters(redis_client)
                auth_use_case = AuthService(
                    core.auth_repository,
                    Argon2PasswordHasher(),
                    org_reader=org_repository,
                    feishu_client=feishu_client,
                    oauth_state_store=oauth_state_store,
                    mock_enabled=auth_settings.mock_enabled,
                    login_account_limiter=rate_limiters.login_account,
                    login_ip_limiter=rate_limiters.login_ip,
                    change_password_limiter=rate_limiters.change_password,
                )
            else:
                auth_use_case = AuthService(
                    core.auth_repository,
                    Argon2PasswordHasher(),
                    org_reader=org_repository,
                    feishu_client=feishu_client,
                    oauth_state_store=oauth_state_store,
                    mock_enabled=auth_settings.mock_enabled,
                )
        if auth_use_case is not None:
            # 复用已解析的共享 Redis 客户端；未配置时直接降级为 None，不再二次探测环境变量。
            run_transport = build_run_transport(redis_client) if redis_client is not None else None
            run_stream_service = RunStreamService(
                core.run_repository,
                auth_use_case,
                run_transport,
            )
        if needs_file_storage:
            if shared_storage_client is not None:
                file_object_store = MinioFileObjectStore(object_client=shared_storage_client)
            else:
                # 仅文件/技能侧自建时复用前置已校验配置，独占底层客户端。
                assert file_storage_config is not None
                file_object_store = MinioFileObjectStore(file_storage_config)
            if session_file_use_case is None:
                session_file_use_case = build_session_file_service(
                    database.session_factory,
                    file_object_store,
                )
        # 技能服务默认绑定统一对象存储；数据库就绪且未注入替身时始终装配。
        if skill_use_case is None:
            skill_use_case = build_skill_service(
                skill_repository=core.skill_repository,
                object_store=file_object_store,
                auth_repository=core.auth_repository,
                access_policy=core.access_policy,
                python_executable=file_settings.python_executable,
            )

    # 上传校验器只依赖解析器本身；挂载 knowledge 路由时一并注入。
    document_validator: DocumentFileValidator | None = (
        LocalDocumentParser(ocr_languages="ch") if knowledge_use_case is not None else None
    )
    return ApiDependencies(
        database=database,
        run_transport=run_transport,
        query_services=query_services,
        project_service=project_use_case,
        message_service=message_use_case,
        session_service=session_use_case,
        run_service=run_use_case,
        auth_service=auth_use_case,
        tool_manager=mcp_use_case,
        model_profile_service=model_profile_use_case,
        system_prompt_service=system_prompt_use_case,
        preference_service=preference_use_case,
        knowledge_service=knowledge_use_case,
        knowledge_repository=knowledge_repository,
        knowledge_originals=knowledge_originals,
        run_stream_service=run_stream_service,
        document_validator=document_validator,
        file_object_store=file_object_store,
        skill_service=skill_use_case,
        session_file_service=session_file_use_case,
        subagent_service=subagent_use_case,
        org_service=org_use_case,
        org_repository=org_repository,
        feishu_client=feishu_client,
        auth_mock_enabled=auth_settings.mock_enabled,
        redis_client=redis_client,
        storage_client=shared_storage_client,
    )


__all__ = ["ApiDependencies", "build_api_dependencies"]
