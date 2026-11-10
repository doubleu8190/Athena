"""API 进程的组合根：集中装配数据库、Redis、LLM 与查询用例。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.llm import OpenAIResponsesAdapter
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.mcp.repository import SqlAlchemyMcpRepository
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.project.repository import SqlAlchemyProjectRepository
from adapter.run.redis_transport import RedisRunTransport
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session import SqlAlchemySessionRepository
from app.auth.service import AuthService
from app.knowledge.service import KnowledgeService
from app.llm import LlmService, ModelProfileService
from app.memory.service import MemoryService
from app.message.service import MessageService
from app.project.service import ProjectService
from app.run.service import RunService
from app.run.stream import RunStreamService
from app.session.service import SessionService
from app.tool import ToolManager
from bootstrap.config import env_csv_set, env_flag
from bootstrap.database import build_database
from bootstrap.knowledge import validate_knowledge_storage_configuration
from bootstrap.redis import build_run_transport
from bootstrap.tool import QueryServiceBundle, build_query_services
from domain.knowledge.ports import DocumentFileValidator, KnowledgeRepositoryPort, OriginalStore
from domain.mcp import McpSecretError
from infrastructure.database import Database
from infrastructure.knowledge.parsers import LocalDocumentParser
from infrastructure.mcp.discovery import McpDiscoveryClient
from infrastructure.security.mcp_secrets import McpSecretCipher
from infrastructure.security.password import Argon2PasswordHasher

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ApiDependencies:
    """API 进程持有并负责清理的进程级资源。"""

    database: Database | None
    llm_adapter: OpenAIResponsesAdapter | None
    run_transport: RedisRunTransport | None
    query_services: QueryServiceBundle | None
    project_service: ProjectService | None
    message_service: MessageService | None
    session_service: SessionService | None
    run_service: RunService | None
    auth_service: AuthService | None
    tool_manager: ToolManager | None
    llm_service: LlmService | None
    model_profile_service: ModelProfileService | None
    memory_service: MemoryService | None
    knowledge_service: KnowledgeService | None
    knowledge_repository: KnowledgeRepositoryPort | None
    knowledge_originals: OriginalStore | None
    run_stream_service: RunStreamService | None
    document_validator: DocumentFileValidator | None

    async def start(self) -> None:
        """在首个请求前启动共享查询资源。"""
        if self.query_services is not None:
            await self.query_services.start()

    async def aclose(self) -> None:
        """按依赖逆序释放资源；单项失败不阻断后续清理。"""
        if self.run_transport is not None:
            try:
                await self.run_transport.aclose()
            except Exception as error:
                logger.error(
                    "run_redis_shutdown_failed exception_type=%s",
                    type(error).__name__,
                )
        if self.llm_adapter is not None:
            try:
                await self.llm_adapter.aclose()
            except Exception:
                logger.error("llm_shutdown_failed code=llm_cleanup_failed")
        if self.query_services is not None:
            try:
                await self.query_services.aclose()
            except Exception as error:
                logger.error(
                    "query_service_shutdown_failed exception_type=%s",
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
    llm_service: LlmService | None = None,
    model_profile_service: ModelProfileService | None = None,
    memory_service: MemoryService | None = None,
    knowledge_service: KnowledgeService | None = None,
) -> ApiDependencies:
    """按环境变量与注入参数组装 API 进程依赖。

    注入的用例保持调用方所有权，不在此关闭；只有本函数自行创建的资源
    进入返回值的清理生命周期。
    """
    database: Database | None = None
    project_use_case = project_service
    message_use_case = message_service
    session_use_case = session_service
    run_use_case = run_service
    auth_use_case = auth_service
    database_url = os.getenv("ATHENA_DATABASE_URL")
    mcp_use_case = tool_manager
    llm_use_case = llm_service
    llm_adapter: OpenAIResponsesAdapter | None = None
    model_profile_use_case = model_profile_service
    memory_use_case = memory_service
    memory_enabled = memory_service is not None or env_flag("ATHENA_MEMORY_ENABLED", default=False)
    knowledge_enabled = knowledge_service is not None or env_flag(
        "ATHENA_KNOWLEDGE_ENABLED", default=False
    )
    knowledge_use_case: KnowledgeService | None = knowledge_service
    knowledge_repository: KnowledgeRepositoryPort | None = None
    knowledge_originals: OriginalStore | None = None
    query_services: QueryServiceBundle | None = None
    memory_owned = memory_service is None and memory_enabled
    knowledge_owned = knowledge_service is None and knowledge_enabled
    run_stream_service: RunStreamService | None = None
    run_transport: RedisRunTransport | None = None
    if memory_owned and not database_url:
        raise RuntimeError("memory_database_required")
    if knowledge_owned and not database_url:
        raise RuntimeError("knowledge_database_required")
    if knowledge_owned:
        # 在创建任何数据库、Redis 或模型资源前失败，避免遗留半组装资源。
        # 同一份环境会由 build_query_services 再次解析。
        validate_knowledge_storage_configuration()
    # LLM 配置由管理员持久化；部署环境只提供加密主密钥，不提供模型凭据。
    secret_cipher: McpSecretCipher | None = None
    mcp_discovery: McpDiscoveryClient | None = None
    if database_url and mcp_use_case is None:
        # 在创建数据库连接池前验证 MCP 密钥，防止失败组装遗留资源。
        secret_cipher = McpSecretCipher.from_environment()
        allowed_private_hosts = env_csv_set("ATHENA_MCP_ALLOWED_PRIVATE_HOSTS")
        stdio_names = env_csv_set("ATHENA_MCP_STDIO_BASE_ENV_NAMES")
        base_environment = {name: os.environ[name] for name in stdio_names if name in os.environ}
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
        or project_use_case is None
        or message_use_case is None
        or session_use_case is None
        or run_use_case is None
        or mcp_use_case is None
        or memory_owned
        # 注入 KnowledgeService 时，项目绑定与查询服务仍共享 API 数据库会话工厂。
        or knowledge_enabled
    ):
        database = build_database(database_url)
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
        if auth_use_case is None:
            auth_use_case = AuthService(auth_repository, Argon2PasswordHasher())
        if session_use_case is None:
            session_use_case = SessionService(session_repository)
        if project_use_case is None:
            project_use_case = ProjectService(project_repository)
        if message_use_case is None:
            message_repository = SqlAlchemyMessageRepository(database.session_factory)
            message_use_case = MessageService(message_repository)
        else:
            message_repository = SqlAlchemyMessageRepository(database.session_factory)
        run_repository = SqlAlchemyRunRepository(database.session_factory, message_repository)
        if run_use_case is None:
            run_use_case = RunService(run_repository)
        if auth_use_case is not None:
            # Redis 未配置时保持为 None，Run 流式服务降级为仅持久化事件。
            run_transport = build_run_transport()
            run_stream_service = RunStreamService(
                run_repository,
                auth_use_case,
                run_transport,
            )
        if mcp_use_case is None:
            if secret_cipher is None or mcp_discovery is None:
                raise McpSecretError("mcp_secret_key_missing")
            mcp_repository = SqlAlchemyMcpRepository(database.session_factory, secret_cipher)
            mcp_use_case = ToolManager(
                mcp_repository,
                mcp_repository,
                mcp_discovery,
                auth_repository,
            )
        query_services = build_query_services(
            database.session_factory,
            projects=project_use_case,
            memory_service=memory_use_case,
            knowledge_service=knowledge_use_case,
        )
        memory_use_case = query_services.memory
        knowledge_use_case = query_services.knowledge
        knowledge_repository = query_services.knowledge_repository
        knowledge_originals = query_services.knowledge_original_store
    # 上传校验器只依赖解析器本身；挂载 knowledge 路由时一并注入。
    document_validator: DocumentFileValidator | None = (
        LocalDocumentParser(ocr_languages="ch") if knowledge_use_case is not None else None
    )
    return ApiDependencies(
        database=database,
        llm_adapter=llm_adapter,
        run_transport=run_transport,
        query_services=query_services,
        project_service=project_use_case,
        message_service=message_use_case,
        session_service=session_use_case,
        run_service=run_use_case,
        auth_service=auth_use_case,
        tool_manager=mcp_use_case,
        llm_service=llm_use_case,
        model_profile_service=model_profile_use_case,
        memory_service=memory_use_case,
        knowledge_service=knowledge_use_case,
        knowledge_repository=knowledge_repository,
        knowledge_originals=knowledge_originals,
        run_stream_service=run_stream_service,
        document_validator=document_validator,
    )


__all__ = ["ApiDependencies", "build_api_dependencies"]
