"""应用组装入口。"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.llm import OpenAIResponsesAdapter
from adapter.mcp.repository import SqlAlchemyMcpRepository
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.project.repository import SqlAlchemyProjectRepository
from adapter.session import SqlAlchemySessionRepository
from api.auth import AuthCookieConfig, build_auth_router
from api.health import router as health_router
from api.mcp import build_mcp_router
from api.messages import build_message_router
from api.projects import build_project_router
from api.security import DEFAULT_COOKIE_NAME
from api.sessions import build_session_router
from api.tools import build_tools_router
from app.auth.service import AuthService
from app.llm import LlmService, parse_profiles
from app.message.service import MessageService
from app.project.service import ProjectService
from app.session.service import SessionService
from app.tool import ToolManager
from domain.mcp import McpSecretError
from infrastructure.database import (
    DEFAULT_DATABASE_MAX_OVERFLOW,
    DEFAULT_DATABASE_POOL_SIZE,
    Database,
)
from infrastructure.mcp.discovery import McpDiscoveryClient
from infrastructure.security.mcp_secrets import McpSecretCipher
from infrastructure.security.password import Argon2PasswordHasher

logger = logging.getLogger(__name__)

_DATABASE_BACKED_ROUTE_PREFIXES = (
    "/api/v1/auth",
    "/api/v1/projects",
    "/api/v1/sessions",
    "/api/v1/admin",
    "/api/v1/tools",
)


async def _handle_database_unavailable(request: Request, _error: Exception) -> JSONResponse:
    """隐藏驱动细节并返回统一的服务不可用响应。"""
    logger.error("database_unavailable path=%s", request.url.path)
    return JSONResponse(
        status_code=503,
        content={"detail": "database_unavailable"},
    )


async def _handle_os_error(request: Request, error: Exception) -> JSONResponse:
    """只把数据库路由的传输层 OSError 映射为服务不可用。

    当前数据库支持认证、项目和 Session（含消息读取）路由。未来加入文件或
    外部网络路由时，必须同步收窄此路径集合，不能让全局 OSError 处理器吞掉
    其他依赖的传输故障。
    """
    if not any(
        request.url.path == prefix or request.url.path.startswith(f"{prefix}/")
        for prefix in _DATABASE_BACKED_ROUTE_PREFIXES
    ):
        raise error
    return await _handle_database_unavailable(request, error)


def create_app(
    project_service: ProjectService | None = None,
    message_service: MessageService | None = None,
    session_service: SessionService | None = None,
    auth_service: AuthService | None = None,
    *,
    tool_manager: ToolManager | None = None,
    llm_service: LlmService | None = None,
    auth_cookie_secure: bool | None = None,
    public_origin: str | None = None,
) -> FastAPI:
    """创建独立的 HTTP 应用实例。

    参数：project_service、message_service、session_service、auth_service 为
    测试或嵌入场景提供已组装用例；auth_cookie_secure 和 public_origin 默认读取
    ATHENA_COOKIE_SECURE、ATHENA_PUBLIC_ORIGIN。数据库用例默认按
    ATHENA_DATABASE_URL 创建适配器。
    返回：FastAPI，已挂载可用路由的应用。
    异常：数据库 URL 无效时可能在创建引擎阶段抛出依赖异常。
    """
    database: Database | None = None
    project_use_case = project_service
    message_use_case = message_service
    session_use_case = session_service
    auth_use_case = auth_service
    database_url = os.getenv("ATHENA_DATABASE_URL")
    mcp_use_case = tool_manager
    llm_use_case = llm_service
    llm_adapter: OpenAIResponsesAdapter | None = None
    if llm_use_case is None:
        llm_profiles = parse_profiles()
        if llm_profiles:
            llm_adapter = OpenAIResponsesAdapter(llm_profiles)
            llm_use_case = LlmService(llm_adapter, llm_profiles)
    secret_cipher: McpSecretCipher | None = None
    mcp_discovery: McpDiscoveryClient | None = None
    if database_url and mcp_use_case is None:
        # 在创建数据库连接池前验证 MCP 密钥，防止失败组装遗留资源。
        secret_cipher = McpSecretCipher.from_environment()
        allowed_private_hosts = _env_csv_set("ATHENA_MCP_ALLOWED_PRIVATE_HOSTS")
        stdio_names = _env_csv_set("ATHENA_MCP_STDIO_BASE_ENV_NAMES")
        base_environment = {name: os.environ[name] for name in stdio_names if name in os.environ}
        mcp_discovery = McpDiscoveryClient(
            allowed_private_hosts=allowed_private_hosts,
            stdio_base_environment=base_environment,
            stdio_allowed_environment_names=stdio_names,
        )
    if database_url and (
        auth_use_case is None
        or project_use_case is None
        or message_use_case is None
        or session_use_case is None
        or mcp_use_case is None
    ):
        database = Database(
            database_url,
            pool_size=_env_int(
                "ATHENA_DATABASE_POOL_SIZE",
                default=DEFAULT_DATABASE_POOL_SIZE,
                minimum=1,
            ),
            max_overflow=_env_int(
                "ATHENA_DATABASE_MAX_OVERFLOW",
                default=DEFAULT_DATABASE_MAX_OVERFLOW,
                minimum=0,
            ),
        )
        project_repository = SqlAlchemyProjectRepository(database.session_factory)
        session_repository = SqlAlchemySessionRepository(database.session_factory)
        auth_repository = SqlAlchemyAuthRepository(database.session_factory)
        if auth_use_case is None:
            auth_use_case = AuthService(auth_repository, Argon2PasswordHasher())
        if session_use_case is None:
            session_use_case = SessionService(session_repository)
        if project_use_case is None:
            project_use_case = ProjectService(project_repository)
        if message_use_case is None:
            message_use_case = MessageService(SqlAlchemyMessageRepository(database.session_factory))
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

    if auth_cookie_secure is None:
        auth_cookie_secure = _env_flag("ATHENA_COOKIE_SECURE", default=False)
    configured_public_origin = (
        public_origin.strip()
        if public_origin is not None
        else os.getenv("ATHENA_PUBLIC_ORIGIN", "").strip()
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """在应用退出时释放数据库连接池。"""
        try:
            yield
        finally:
            if llm_adapter is not None:
                try:
                    await llm_adapter.aclose()
                except Exception:
                    logger.error("llm_shutdown_failed code=llm_cleanup_failed")
            if database is not None:
                try:
                    await database.dispose()
                except Exception:
                    logger.error("database_shutdown_failed")

    app = FastAPI(title="Athena", version="0.1.0", lifespan=lifespan)
    app.add_exception_handler(SQLAlchemyError, _handle_database_unavailable)
    app.add_exception_handler(OSError, _handle_os_error)
    app.state.authenticator = auth_use_case
    app.state.auth_cookie_name = DEFAULT_COOKIE_NAME
    app.state.public_origin = configured_public_origin or None
    app.state.tool_manager = mcp_use_case
    app.state.llm_service = llm_use_case
    app.include_router(health_router)
    if project_use_case is not None:
        app.include_router(build_project_router(project_use_case))
    if session_use_case is not None:
        app.include_router(build_session_router(session_use_case))
    if message_use_case is not None:
        app.include_router(build_message_router(message_use_case))
    if auth_use_case is not None:
        app.include_router(
            build_auth_router(
                auth_use_case,
                cookie=AuthCookieConfig(
                    name=DEFAULT_COOKIE_NAME,
                    secure=auth_cookie_secure,
                ),
            )
        )
    if mcp_use_case is not None:
        app.include_router(build_mcp_router(mcp_use_case))
        app.include_router(build_tools_router(mcp_use_case))
    return app


def _env_flag(name: str, *, default: bool) -> bool:
    """解析严格布尔环境变量，拒绝含糊配置。"""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name}_invalid")


def _env_int(name: str, *, default: int, minimum: int) -> int:
    """解析带下界的环境整数，拒绝 bool 语义和含糊文本。"""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError as error:
        raise ValueError(f"{name}_invalid") from error
    if parsed < minimum:
        raise ValueError(f"{name}_invalid")
    return parsed


def _env_csv_set(name: str) -> frozenset[str]:
    """解析逗号分隔的部署白名单，拒绝空项和非法主机/变量名。"""
    raw = os.getenv(name, "")
    if not raw.strip():
        return frozenset()
    parts = [part.strip() for part in raw.split(",")]
    if any(not part for part in parts):
        raise ValueError(f"{name}_invalid")
    if name == "ATHENA_MCP_ALLOWED_PRIVATE_HOSTS":
        normalized_hosts: set[str] = set()
        try:
            for part in parts:
                try:
                    host = ipaddress.ip_address(part).compressed
                except ValueError:
                    ascii_host = part.encode("idna").decode("ascii").lower()
                    labels = ascii_host.split(".")
                    if len(ascii_host) > 253 or any(
                        not label
                        or len(label) > 63
                        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                        for label in labels
                    ):
                        raise ValueError from None
                    host = ascii_host
                normalized_hosts.add(host)
        except (UnicodeError, ValueError):
            raise ValueError(f"{name}_invalid") from None
        return frozenset(normalized_hosts)
    else:
        valid = all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts)
    if not valid:
        raise ValueError(f"{name}_invalid")
    return frozenset(parts)
