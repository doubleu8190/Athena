"""应用组装入口。"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import shutil
import tempfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.knowledge.repository import KnowledgeRepository
from adapter.llm import OpenAIResponsesAdapter
from adapter.mcp.repository import SqlAlchemyMcpRepository
from adapter.memory.repository import MemoryRepository
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.project.repository import SqlAlchemyProjectRepository
from adapter.session import SqlAlchemySessionRepository
from api.auth import AuthCookieConfig, build_auth_router
from api.health import router as health_router
from api.knowledge import build_knowledge_router
from api.mcp import build_mcp_router
from api.messages import build_message_router
from api.project_knowledge import build_project_knowledge_router
from api.projects import build_project_router
from api.security import DEFAULT_COOKIE_NAME
from api.sessions import build_session_router
from api.tools import build_tools_router
from app.auth.service import AuthService
from app.knowledge.service import KnowledgeService
from app.llm import LlmService, parse_profiles
from app.memory.service import MemoryService
from app.message.service import MessageService
from app.project.knowledge import ProjectKnowledgeService
from app.project.service import ProjectService
from app.session.service import SessionService
from app.tool import ToolManager
from common.ids import new_id
from domain.knowledge.ports import KnowledgeRepositoryPort
from domain.mcp import McpSecretError
from infrastructure.database import (
    DEFAULT_DATABASE_MAX_OVERFLOW,
    DEFAULT_DATABASE_POOL_SIZE,
    Database,
)
from infrastructure.embedding import LocalEmbedding
from infrastructure.embedding.profile import embedding_profile_id
from infrastructure.knowledge import (
    JiebaChineseTokenizer,
    LocalKnowledgeTokenCounter,
    LocalOriginalStore,
)
from infrastructure.mcp.discovery import McpDiscoveryClient
from infrastructure.memory import LocalReranker, LocalTokenCounter
from infrastructure.security.mcp_secrets import McpSecretCipher
from infrastructure.security.password import Argon2PasswordHasher

logger = logging.getLogger(__name__)


class _KnowledgeUploadLimitMiddleware:
    """Bound multipart requests before Starlette parses/spools their fields."""

    _maximum = 22 * 1024 * 1024  # 20 MiB file plus bounded multipart metadata.

    def __init__(
        self,
        app: object,
        repository: KnowledgeRepositoryPort | None = None,
        storage_path: str | None = None,
    ) -> None:
        self.app = app
        self._repository = repository
        self._storage_path = storage_path
        self._local_slots = asyncio.Semaphore(4)

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        path = str(scope.get("path", ""))
        method = str(scope.get("method", ""))
        upload_path = path.startswith("/api/v1/knowledge-bases/") and (
            path.endswith("/documents/upload") or path.endswith("/versions/upload")
        )
        if method != "POST" or not upload_path:
            await self.app(scope, receive, send)  # type: ignore[operator]
            return
        receive_fn = receive
        send_fn = send
        assert callable(receive_fn) and callable(send_fn)
        minimum_free_bytes = _env_int(
            "ATHENA_KNOWLEDGE_UPLOAD_MIN_FREE_BYTES",
            default=384 * 1024 * 1024,
            minimum=1,
        )
        try:
            temp_free = shutil.disk_usage(tempfile.gettempdir()).free
            storage_free = (
                shutil.disk_usage(self._storage_path).free
                if self._storage_path is not None
                else temp_free
            )
            if min(temp_free, storage_free) < minimum_free_bytes:
                await _send_json(send_fn, 503, "knowledge_upload_storage_low")
                return
        except OSError:
            await _send_json(send_fn, 503, "knowledge_upload_storage_unavailable")
            return
        slot_token = new_id("kuslot")
        acquired = False
        if self._repository is None:
            await self._local_slots.acquire()
            acquired = True
        else:
            try:
                acquired = await self._repository.acquire_upload_slot(slot_token, lease_seconds=900)
            except Exception:
                await _send_json(send_fn, 503, "knowledge_upload_admission_unavailable")
                return
        if not acquired:
            await _send_json(send_fn, 429, "knowledge_upload_capacity_exceeded")
            return
        response_started = False

        async def tracked_send(message: dict[str, object]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send_fn(message)

        try:
            async with asyncio.timeout(600):
                with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as body:
                    size = 0
                    while True:
                        message = await receive_fn()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        size += len(chunk)
                        if size > self._maximum:
                            await _send_json(tracked_send, 413, "document_file_too_large")
                            return
                        body.write(chunk)
                        if not message.get("more_body", False):
                            break
                    body.seek(0)
                    first = True

                    async def replay() -> dict[str, object]:
                        nonlocal first
                        if first:
                            first = False
                            return {
                                "type": "http.request",
                                "body": body.read(64 * 1024),
                                "more_body": True,
                            }
                        chunk = body.read(64 * 1024)
                        return {"type": "http.request", "body": chunk, "more_body": bool(chunk)}

                    await self.app(scope, replay, tracked_send)  # type: ignore[operator]
        except TimeoutError:
            if not response_started:
                await _send_json(tracked_send, 504, "knowledge_upload_timeout")
        finally:
            if self._repository is None:
                self._local_slots.release()
            else:
                try:
                    await asyncio.shield(self._repository.release_upload_slot(slot_token))
                except Exception:
                    logger.error("knowledge_upload_slot_release_failed")


async def _send_json(send: object, status: int, detail: str) -> None:
    """发送上传准入或大小限制错误响应。"""
    assert callable(send)
    payload = ('{"detail":"' + detail + '"}').encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def memory_profile_id(model: str, revision: str) -> str:
    """根据模型权重与向量预处理规则生成不可混用的档案标识。"""
    return embedding_profile_id(model, revision)


_DATABASE_BACKED_ROUTE_PREFIXES = (
    "/api/v1/auth",
    "/api/v1/projects",
    "/api/v1/sessions",
    "/api/v1/admin",
    "/api/v1/tools",
    "/api/v1/knowledge-bases",
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
    memory_service: MemoryService | None = None,
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
    memory_embedding: LocalEmbedding | None = None
    memory_reranker: LocalReranker | None = None
    memory_counter: LocalTokenCounter | None = None
    memory_use_case = memory_service
    knowledge_embedding: LocalEmbedding | None = None
    knowledge_counter = None
    memory_enabled = _env_flag("ATHENA_MEMORY_ENABLED", default=False)
    knowledge_enabled = _env_flag("ATHENA_KNOWLEDGE_ENABLED", default=False)
    knowledge_use_case: KnowledgeService | None = None
    knowledge_repository: KnowledgeRepository | None = None
    knowledge_originals: LocalOriginalStore | None = None
    memory_owned = memory_service is None and memory_enabled
    if memory_owned and not database_url:
        raise RuntimeError("memory_database_required")
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
        or memory_owned
        or (knowledge_enabled and knowledge_use_case is None)
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
        if knowledge_enabled:
            storage_root = os.getenv("ATHENA_KNOWLEDGE_STORAGE_ROOT", "./var/knowledge")
            knowledge_originals = LocalOriginalStore(storage_root)
            model_name = os.getenv("ATHENA_KNOWLEDGE_EMBEDDING_MODEL", "BAAI/bge-m3")
            model_revision = os.getenv(
                "ATHENA_KNOWLEDGE_EMBEDDING_REVISION",
                "5617a9f61b028005a4858fdac845db406aefb181",
            )
            knowledge_embedding = LocalEmbedding(
                model_name,
                model_revision,
                os.getenv("ATHENA_KNOWLEDGE_EMBEDDING_DEVICE", "cpu"),
            )
            knowledge_counter = LocalKnowledgeTokenCounter(model_name, model_revision)
            knowledge_repository = KnowledgeRepository(database.session_factory)
            knowledge_use_case = KnowledgeService(
                knowledge_repository,
                JiebaChineseTokenizer(),
                knowledge_embedding,
                knowledge_originals,
                knowledge_counter,
                profile_id=memory_profile_id(model_name, model_revision),
            )
    if memory_owned and database is not None:
        embedding_model = os.getenv("ATHENA_MEMORY_EMBEDDING_MODEL", "BAAI/bge-m3")
        embedding_revision = os.getenv(
            "ATHENA_MEMORY_EMBEDDING_REVISION",
            "5617a9f61b028005a4858fdac845db406aefb181",
        )
        reranker_model = os.getenv("ATHENA_MEMORY_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
        reranker_revision = os.getenv(
            "ATHENA_MEMORY_RERANKER_REVISION",
            "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
        )
        tokenizer_model = os.getenv("ATHENA_MEMORY_TOKENIZER", embedding_model)
        tokenizer_revision = os.getenv("ATHENA_MEMORY_TOKENIZER_REVISION", embedding_revision)
        memory_embedding = LocalEmbedding(
            embedding_model,
            embedding_revision,
            os.getenv("ATHENA_MEMORY_EMBEDDING_DEVICE", "cpu"),
        )
        memory_reranker = LocalReranker(
            reranker_model,
            reranker_revision,
            os.getenv("ATHENA_MEMORY_RERANKER_DEVICE", "cpu"),
        )
        memory_counter = LocalTokenCounter(tokenizer_model, tokenizer_revision)
        memory_profile = memory_profile_id(embedding_model, embedding_revision)
        memory_use_case = MemoryService(
            MemoryRepository(database.session_factory),
            memory_embedding,
            memory_reranker,
            memory_counter,
            profile_id=memory_profile,
            vector_min_score=float(os.getenv("ATHENA_MEMORY_VECTOR_MIN_SCORE", "0.70")),
            timeout_seconds=float(os.getenv("ATHENA_MEMORY_TIMEOUT_SECONDS", "60")),
            rerank_min_score=float(os.getenv("ATHENA_MEMORY_RERANK_MIN_SCORE", "0.30")),
            keyword_fallback_min_score=(
                float(os.environ["ATHENA_MEMORY_KEYWORD_FALLBACK_MIN_SCORE"])
                if "ATHENA_MEMORY_KEYWORD_FALLBACK_MIN_SCORE" in os.environ
                else None
            ),
        )

    if auth_cookie_secure is None:
        auth_cookie_secure = _env_flag("ATHENA_COOKIE_SECURE", default=False)
    configured_public_origin = (
        public_origin.strip()
        if public_origin is not None
        else os.getenv("ATHENA_PUBLIC_ORIGIN", "").strip()
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        """在应用退出时释放数据库连接池。"""
        try:
            if knowledge_counter is not None:
                await knowledge_counter.start()
            if knowledge_embedding is not None:
                await knowledge_embedding.start()
            if memory_owned and memory_embedding is not None and memory_reranker is not None:
                await memory_embedding.start()
                await memory_reranker.start()
                if memory_counter is not None:
                    await memory_counter.start()
            yield
        finally:
            if llm_adapter is not None:
                try:
                    await llm_adapter.aclose()
                except Exception:
                    logger.error("llm_shutdown_failed code=llm_cleanup_failed")
            if memory_owned:
                if memory_embedding is not None:
                    await memory_embedding.aclose()
                if memory_reranker is not None:
                    await memory_reranker.aclose()
                if memory_counter is not None:
                    memory_counter.close()
            if knowledge_embedding is not None:
                await knowledge_embedding.aclose()
            if knowledge_counter is not None:
                knowledge_counter.close()
            if database is not None:
                try:
                    await database.dispose()
                except Exception:
                    logger.error("database_shutdown_failed")

    app = FastAPI(title="Athena", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        _KnowledgeUploadLimitMiddleware,
        repository=knowledge_repository,
        storage_path=(
            os.getenv("ATHENA_KNOWLEDGE_STORAGE_ROOT", "./var/knowledge")
            if knowledge_repository is not None
            else None
        ),
    )
    app.add_exception_handler(SQLAlchemyError, _handle_database_unavailable)
    app.add_exception_handler(OSError, _handle_os_error)
    app.state.authenticator = auth_use_case
    app.state.auth_cookie_name = DEFAULT_COOKIE_NAME
    app.state.public_origin = configured_public_origin or None
    app.state.tool_manager = mcp_use_case
    app.state.llm_service = llm_use_case
    app.state.memory_service = memory_use_case
    app.state.knowledge_service = knowledge_use_case
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
    if knowledge_use_case is not None:
        app.state.knowledge_original_store = knowledge_originals
        app.include_router(build_knowledge_router(knowledge_use_case))
        if project_use_case is not None and database is not None:
            app.include_router(
                build_project_knowledge_router(
                    ProjectKnowledgeService(
                        project_use_case, KnowledgeRepository(database.session_factory)
                    ),
                    project_use_case,
                )
            )
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
