"""API 进程的组合根：集中装配数据库、Redis、LLM 与查询用例。"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from redis.asyncio import Redis

from adapter.access import SqlAlchemyAccessControlRepository
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.file import MinioFileObjectStore, SqlAlchemySessionFileRepository
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.mcp.repository import SqlAlchemyMcpRepository
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.org import SqlAlchemyOrgRepository
from adapter.preference import SqlAlchemyPreferenceRepository
from adapter.project.repository import SqlAlchemyProjectRepository
from adapter.run.redis_transport import RedisRunTransport
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session import SqlAlchemySessionRepository
from adapter.skill import SqlAlchemySkillRepository
from adapter.subagent import SqlAlchemySubagentModelChecker, SqlAlchemySubagentRepository
from adapter.system_prompt import SqlAlchemySystemPromptRepository
from app.access import AccessPolicyService
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
from app.skill import DependencyChecker, SkillService
from app.subagent import DefaultSubagentToolCatalog, SubagentService
from app.system_prompt import SystemPromptService
from app.tool import ToolManager
from bootstrap.config import env_csv_set, env_flag
from bootstrap.database import build_database
from bootstrap.knowledge import (
    validate_knowledge_embedding_configuration,
    validate_knowledge_storage_configuration,
)
from bootstrap.redis import (
    build_auth_rate_limiters,
    build_oauth_state_store,
    build_redis_client,
    build_run_transport,
    is_production_environment,
    validate_auth_rate_limit_configuration,
    validate_feishu_state_store_configuration,
)
from bootstrap.tool import QueryServiceBundle, build_query_services
from domain.file.ports import FileObjectStore
from domain.knowledge.ports import (
    DocumentFileValidator,
    KnowledgeRepositoryPort,
    KnowledgeUploadSlotRepository,
    OriginalStore,
)
from domain.mcp import McpSecretError
from domain.org import OrgRepository
from infrastructure.database import Database
from infrastructure.feishu import FeishuClient, FeishuConfig
from infrastructure.knowledge.parsers import LocalDocumentParser
from infrastructure.mcp.discovery import McpDiscoveryClient
from infrastructure.security.mcp_secrets import McpSecretCipher
from infrastructure.security.password import Argon2PasswordHasher
from infrastructure.storage.config import has_object_storage_env

logger = logging.getLogger(__name__)


def _file_storage_enabled() -> bool:
    """优先按显式 ATHENA_FILES_ENABLED 开关启用；未显式设置时按统一 ATHENA_STORAGE_* 判定。"""
    if "ATHENA_FILES_ENABLED" in os.environ:
        return env_flag("ATHENA_FILES_ENABLED", default=False)
    # 任一统一对象存储连接项存在即视为已配置，让不完整配置在启动时明确失败；
    # 完全没有对象存储配置的开发/旧测试实例则不挂载相关路由。
    return has_object_storage_env()


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
    knowledge_upload_slots: KnowledgeUploadSlotRepository | None = None
    subagent_service: SubagentService | None = None
    org_service: OrgService | None = None
    org_repository: OrgRepository | None = None
    feishu_client: FeishuClient | None = None
    auth_mock_enabled: bool = False
    redis_client: Redis | None = None

    async def start(self) -> None:
        """在首个请求前启动共享查询资源，并在非生产 Mock 模式下幂等预置组织与账号种子。"""
        if self.auth_mock_enabled and self.org_repository is not None:
            await seed_mock_organization(self.org_repository)
        if self.file_object_store is not None:
            await self.file_object_store.start()
        if self.query_services is not None:
            await self.query_services.start()

    async def aclose(self) -> None:
        """按依赖逆序释放资源；单项失败不阻断后续清理。"""
        if self.feishu_client is not None:
            try:
                await self.feishu_client.aclose()
            except Exception as error:
                logger.error(
                    "feishu_client_shutdown_failed exception_type=%s",
                    type(error).__name__,
                )
        if self.run_transport is not None:
            try:
                await self.run_transport.aclose()
            except Exception as error:
                logger.error(
                    "run_redis_shutdown_failed exception_type=%s",
                    type(error).__name__,
                )
        if self.redis_client is not None:
            try:
                async with asyncio.timeout(1.0):
                    await self.redis_client.aclose()
            except Exception as error:
                logger.error(
                    "redis_client_shutdown_failed exception_type=%s",
                    type(error).__name__,
                )
        if self.query_services is not None:
            try:
                await self.query_services.aclose()
            except Exception as error:
                logger.error(
                    "query_service_shutdown_failed exception_type=%s",
                    type(error).__name__,
                )
        if self.file_object_store is not None:
            try:
                await self.file_object_store.aclose()
            except Exception as error:
                logger.error(
                    "file_storage_shutdown_failed exception_type=%s",
                    type(error).__name__,
                )
        if self.database is not None:
            try:
                await self.database.dispose()
            except Exception:
                logger.error("database_shutdown_failed")


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
    """按环境变量与注入参数组装 API 进程依赖。

    注入的用例保持调用方所有权，不在此关闭；只有本函数自行创建的资源
    进入返回值的清理生命周期。
    """
    # 强制生产安全护栏：生产环境严禁开启本地 Mock 快捷登录，在分配任何连接池前立即阻断启动。
    auth_mock_enabled = env_flag("ATHENA_AUTH_MOCK_ENABLED", default=False)
    if is_production_environment() and auth_mock_enabled:
        raise RuntimeError("auth_mock_forbidden_in_production")

    feishu_enabled = env_flag("ATHENA_FEISHU_ENABLED", default=False)
    feishu_config: FeishuConfig | None = None
    if feishu_enabled:
        # 生产环境开启飞书 SSO 必须提供 Redis 一次性 state 存储，并校验凭据与 HTTPS 配置。
        validate_feishu_state_store_configuration()
        feishu_config = FeishuConfig.from_environment(
            is_production=is_production_environment(),
        )

    database: Database | None = None
    access_policy: AccessPolicyService | None = None
    project_use_case = project_service
    message_use_case = message_service
    session_use_case = session_service
    run_use_case = run_service
    auth_use_case = auth_service
    org_use_case = org_service
    org_repository: OrgRepository | None = None
    feishu_client: FeishuClient | None = None
    database_url = os.getenv("ATHENA_DATABASE_URL")
    mcp_use_case = tool_manager
    model_profile_use_case = model_profile_service
    system_prompt_use_case = system_prompt_service
    subagent_use_case = subagent_service
    preference_use_case = preference_service
    knowledge_enabled = knowledge_service is not None or env_flag(
        "ATHENA_KNOWLEDGE_ENABLED", default=False
    )
    knowledge_use_case: KnowledgeService | None = knowledge_service
    knowledge_repository: KnowledgeRepositoryPort | None = None
    knowledge_upload_slots: KnowledgeUploadSlotRepository | None = None
    knowledge_originals: OriginalStore | None = None
    query_services: QueryServiceBundle | None = None
    knowledge_owned = knowledge_service is None and knowledge_enabled
    run_stream_service: RunStreamService | None = None
    run_transport: RedisRunTransport | None = None
    redis_client: Redis | None = None
    if auth_use_case is None:
        # 生产环境必须在启动初期拒绝缺失 Redis 限流的配置，避免分配数据库连接池后再失败。
        validate_auth_rate_limit_configuration()
    if knowledge_owned and not database_url:
        raise RuntimeError("knowledge_database_required")
    if knowledge_owned:
        # 在创建任何数据库、Redis 或模型资源前校验统一存储与远程 Embedding 配置，
        # 避免遗留半组装资源；同一份环境会由 build_query_services 再次解析。
        validate_knowledge_storage_configuration()
        validate_knowledge_embedding_configuration()
    # LLM 配置由管理员持久化；部署环境只提供加密主密钥，不提供模型凭据。
    secret_cipher: McpSecretCipher | None = None
    mcp_discovery: McpDiscoveryClient | None = None
    if database_url and (mcp_use_case is None or subagent_use_case is None):
        # 在创建数据库连接池前验证 MCP 密钥，防止失败组装遗留资源。
        # 注入 ToolManager 时仍需为默认 Subagent 目录建立同库只读仓储。
        secret_cipher = McpSecretCipher.from_environment()
        if mcp_use_case is None:
            allowed_private_hosts = env_csv_set("ATHENA_MCP_ALLOWED_PRIVATE_HOSTS")
            stdio_names = env_csv_set("ATHENA_MCP_STDIO_BASE_ENV_NAMES")
            base_environment = {
                name: os.environ[name] for name in stdio_names if name in os.environ
            }
            mcp_discovery = McpDiscoveryClient(
                allowed_private_hosts=allowed_private_hosts,
                stdio_base_environment=base_environment,
                stdio_allowed_environment_names=stdio_names,
            )
    if database_url and model_profile_use_case is None and secret_cipher is None:
        # LLM 加密密钥未配置时仍允许非 MCP 功能启动；模型调用会安全地保持不可用。
        try:
            secret_cipher = McpSecretCipher.from_environment()
        except McpSecretError:
            secret_cipher = None
    if database_url and (
        auth_use_case is None
        or org_use_case is None
        or project_use_case is None
        or message_use_case is None
        or session_use_case is None
        or run_use_case is None
        or mcp_use_case is None
        # 注入 KnowledgeService 时，项目绑定与查询服务仍共享 API 数据库会话工厂。
        or knowledge_enabled
        or system_prompt_use_case is None
        or preference_use_case is None
        or subagent_use_case is None
    ):
        database = build_database(database_url)
        # 在 API 进程内共享同一统一权限引擎，确保四条业务线实时共用组织树与授权表判定。
        access_policy = AccessPolicyService(
            SqlAlchemyAccessControlRepository(database.session_factory)
        )
        project_repository = SqlAlchemyProjectRepository(database.session_factory)
        model_profile_repository = SqlAlchemyModelProfileRepository(
            database.session_factory, secret_cipher
        )
        if model_profile_use_case is None:
            model_profile_use_case = ModelProfileService(
                model_profile_repository, SqlAlchemyAuthRepository(database.session_factory)
            )
        session_repository = SqlAlchemySessionRepository(database.session_factory)
        auth_repository = SqlAlchemyAuthRepository(database.session_factory)
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
                auth_repository,
                feishu_sync_service=feishu_sync_service,
            )
        if system_prompt_use_case is None:
            system_prompt_use_case = SystemPromptService(
                SqlAlchemySystemPromptRepository(database.session_factory),
                auth_repository,
            )
        if preference_use_case is None:
            preference_use_case = PreferenceService(
                SqlAlchemyPreferenceRepository(database.session_factory)
            )
        if session_use_case is None:
            session_use_case = SessionService(session_repository)
        if project_use_case is None:
            project_use_case = ProjectService(project_repository)
        if message_use_case is None:
            message_repository = SqlAlchemyMessageRepository(database.session_factory)
            message_use_case = MessageService(message_repository)
        else:
            message_repository = SqlAlchemyMessageRepository(database.session_factory)
        mcp_repository: SqlAlchemyMcpRepository | None = None
        if mcp_use_case is None or subagent_use_case is None:
            if secret_cipher is None:
                raise McpSecretError("mcp_secret_key_missing")
            # 子代理目录与服务写入共享同一 API 数据库，避免目录读取使用独立快照。
            mcp_repository = SqlAlchemyMcpRepository(database.session_factory, secret_cipher)
        if mcp_use_case is None:
            if mcp_discovery is None or mcp_repository is None:
                raise McpSecretError("mcp_secret_key_missing")
            mcp_use_case = ToolManager(
                mcp_repository,
                mcp_discovery,
                auth_repository,
                access_policy=access_policy,
            )
        if subagent_use_case is None:
            subagent_use_case = SubagentService(
                SqlAlchemySubagentRepository(database.session_factory),
                auth_repository,
                access_policy=access_policy,
                model_checker=SqlAlchemySubagentModelChecker(database.session_factory),
                tool_catalog=DefaultSubagentToolCatalog(mcp_repository),
            )
        query_services = build_query_services(
            database.session_factory,
            projects=project_use_case,
            knowledge_service=knowledge_use_case,
            access_policy=access_policy,
        )
        knowledge_use_case = query_services.knowledge
        knowledge_repository = query_services.knowledge_repository
        knowledge_upload_slots = query_services.knowledge_upload_slots
        knowledge_originals = query_services.knowledge_original_store
        run_repository = SqlAlchemyRunRepository(database.session_factory, message_repository)
        if run_use_case is None:
            run_use_case = RunService(run_repository)
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
                    auth_repository,
                    Argon2PasswordHasher(),
                    org_reader=org_repository,
                    feishu_client=feishu_client,
                    oauth_state_store=oauth_state_store,
                    mock_enabled=auth_mock_enabled,
                    login_account_limiter=rate_limiters.login_account,
                    login_ip_limiter=rate_limiters.login_ip,
                    change_password_limiter=rate_limiters.change_password,
                )
            else:
                auth_use_case = AuthService(
                    auth_repository,
                    Argon2PasswordHasher(),
                    org_reader=org_repository,
                    feishu_client=feishu_client,
                    oauth_state_store=oauth_state_store,
                    mock_enabled=auth_mock_enabled,
                )
        if auth_use_case is not None:
            # Redis 未配置时保持为 None，Run 流式服务降级为仅持久化事件。
            run_transport = (
                build_run_transport(redis_client)
                if redis_client is not None
                else build_run_transport()
            )
            run_stream_service = RunStreamService(
                run_repository,
                auth_use_case,
                run_transport,
            )
    # 上传校验器只依赖解析器本身；挂载 knowledge 路由时一并注入。
    document_validator: DocumentFileValidator | None = (
        LocalDocumentParser(ocr_languages="ch") if knowledge_use_case is not None else None
    )
    file_object_store: FileObjectStore | None = None
    skill_use_case = skill_service
    session_file_use_case = session_file_service
    files_enabled = _file_storage_enabled()
    if (
        database is not None
        and files_enabled
        and (skill_use_case is None or session_file_use_case is None)
    ):
        file_object_store = MinioFileObjectStore.from_environment(deferred=True)
        if skill_use_case is None:
            skill_use_case = SkillService(
                SqlAlchemySkillRepository(database.session_factory),
                file_object_store,
                auth_repository,
                access_policy=access_policy,
                dependency_checker=DependencyChecker(
                    python_executable=Path(
                        os.getenv("ATHENA_SKILL_PYTHON_EXECUTABLE", sys.executable)
                    )
                ),
            )
        if session_file_use_case is None:
            session_file_use_case = SessionFileService(
                SqlAlchemySessionFileRepository(database.session_factory),
                file_object_store,
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
        knowledge_upload_slots=knowledge_upload_slots,
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
        auth_mock_enabled=auth_mock_enabled,
        redis_client=redis_client,
    )


__all__ = ["ApiDependencies", "build_api_dependencies"]
