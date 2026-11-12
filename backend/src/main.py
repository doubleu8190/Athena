"""应用组装入口。"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from adapter.file.storage import FileStorageError
from api.auth import AuthCookieConfig, build_auth_router
from api.file import build_session_file_router
from api.health import router as health_router
from api.knowledge import build_knowledge_router
from api.llm import build_llm_admin_router
from api.mcp import build_mcp_router
from api.messages import build_message_router
from api.project_knowledge import build_project_knowledge_router
from api.projects import build_project_router
from api.runs import build_run_router
from api.security import DEFAULT_COOKIE_NAME
from api.sessions import build_session_router
from api.skill import build_skill_admin_router, build_skill_user_router
from api.system_prompt import build_system_prompt_admin_router
from api.tools import build_tools_router
from app.auth.service import AuthService
from app.file import SessionFileService
from app.knowledge.service import KnowledgeService
from app.llm import ModelProfileService
from app.memory.service import MemoryService
from app.message.service import MessageService
from app.project.service import ProjectService
from app.run.service import RunService
from app.session.service import SessionService
from app.skill import SkillService
from app.system_prompt import SystemPromptService
from app.tool import ToolManager
from bootstrap.api import ApiDependencies, build_api_dependencies
from bootstrap.config import env_flag, env_int
from common.ids import new_id
from domain.knowledge.ports import KnowledgeRepositoryPort

logger = logging.getLogger(__name__)


class _KnowledgeUploadLimitMiddleware:
    """在 Starlette 解析 multipart 字段前限制原始请求体。"""

    _maximum = 22 * 1024 * 1024  # 20 MiB file plus bounded multipart metadata.

    def __init__(
        self,
        app: object,
        repository: KnowledgeRepositoryPort | None = None,
    ) -> None:
        self.app = app
        self._repository = repository
        self._local_slots = asyncio.Semaphore(4)

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        path = str(scope.get("path", ""))
        method = str(scope.get("method", ""))
        knowledge_upload = path.startswith("/api/v1/knowledge-bases/") and (
            path.endswith("/documents/upload") or path.endswith("/versions/upload")
        )
        skill_upload = path == "/api/v1/admin/skills/import" or (
            path.startswith("/api/v1/admin/skills/") and path.endswith("/versions")
        )
        session_file_upload = path.startswith("/api/v1/sessions/") and path.endswith("/files")
        upload_kind = (
            "knowledge"
            if knowledge_upload
            else "skill"
            if skill_upload
            else "session_file"
            if session_file_upload
            else None
        )
        if method != "POST" or upload_kind is None:
            await self.app(scope, receive, send)  # type: ignore[operator]
            return
        maximum = (
            26 * 1024 * 1024
            if upload_kind == "skill"
            else 52 * 1024 * 1024
            if upload_kind == "session_file"
            else self._maximum
        )
        overflow_code = (
            "skill_zip_too_large"
            if upload_kind == "skill"
            else "document_file_too_large"
        )
        receive_fn = receive
        send_fn = send
        assert callable(receive_fn) and callable(send_fn)
        if upload_kind == "knowledge":
            minimum_free_bytes = env_int(
                "ATHENA_KNOWLEDGE_UPLOAD_MIN_FREE_BYTES",
                default=384 * 1024 * 1024,
                minimum=1,
            )
            try:
                temp_free = shutil.disk_usage(tempfile.gettempdir()).free
                if temp_free < minimum_free_bytes:
                    await _send_json(send_fn, 503, "knowledge_upload_storage_low")
                    return
            except OSError:
                await _send_json(send_fn, 503, "knowledge_upload_storage_unavailable")
                return
        slot_token = new_id("kuslot")
        acquired = False
        if upload_kind != "knowledge" or self._repository is None:
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
                        if size > maximum:
                            await _send_json(tracked_send, 413, overflow_code)
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
            if upload_kind != "knowledge" or self._repository is None:
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


async def _handle_file_storage_unavailable(
    request: Request, error: Exception
) -> JSONResponse:
    """只映射专用对象存储异常，不吞掉其他 RuntimeError。"""
    del error
    logger.error("file_storage_unavailable path=%s", request.url.path)
    return JSONResponse(
        status_code=503,
        content={"detail": "file_storage_unavailable"},
    )


async def _handle_request_validation_error(
    request: Request, error: Exception
) -> JSONResponse:
    """去除请求值，避免校验响应回显管理员提交的 API Key。"""
    del request
    if not isinstance(error, RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": "request_invalid"})
    safe_errors = [
        {key: value for key, value in item.items() if key != "input"}
        for item in error.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": safe_errors})


def create_app(
    project_service: ProjectService | None = None,
    message_service: MessageService | None = None,
    session_service: SessionService | None = None,
    run_service: RunService | None = None,
    auth_service: AuthService | None = None,
    *,
    tool_manager: ToolManager | None = None,
    model_profile_service: ModelProfileService | None = None,
    system_prompt_service: SystemPromptService | None = None,
    memory_service: MemoryService | None = None,
    knowledge_service: KnowledgeService | None = None,
    skill_service: SkillService | None = None,
    session_file_service: SessionFileService | None = None,
    auth_cookie_secure: bool | None = None,
    public_origin: str | None = None,
) -> FastAPI:
    """创建独立的 HTTP 应用实例。

    参数：project_service、message_service、session_service、auth_service 为
    测试或嵌入场景提供已组装用例；auth_cookie_secure 和 public_origin 默认读取
    ATHENA_COOKIE_SECURE、ATHENA_PUBLIC_ORIGIN。其余依赖由 bootstrap.api 按环境
    变量统一装配。
    返回：FastAPI，已挂载可用路由的应用。
    异常：数据库 URL 无效时可能在创建引擎阶段抛出依赖异常。
    """
    deps: ApiDependencies = build_api_dependencies(
        project_service=project_service,
        message_service=message_service,
        session_service=session_service,
        run_service=run_service,
        auth_service=auth_service,
        tool_manager=tool_manager,
        model_profile_service=model_profile_service,
        system_prompt_service=system_prompt_service,
        memory_service=memory_service,
        knowledge_service=knowledge_service,
        skill_service=skill_service,
        session_file_service=session_file_service,
    )
    if auth_cookie_secure is None:
        auth_cookie_secure = env_flag("ATHENA_COOKIE_SECURE", default=False)
    configured_public_origin = (
        public_origin.strip()
        if public_origin is not None
        else os.getenv("ATHENA_PUBLIC_ORIGIN", "").strip()
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        """启动共享查询资源，并在应用退出时释放全部进程级依赖。"""
        try:
            await deps.start()
            yield
        finally:
            await deps.aclose()

    app = FastAPI(title="Athena", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        _KnowledgeUploadLimitMiddleware,
        repository=deps.knowledge_repository,
    )
    app.add_exception_handler(SQLAlchemyError, _handle_database_unavailable)
    app.add_exception_handler(OSError, _handle_os_error)
    app.add_exception_handler(FileStorageError, _handle_file_storage_unavailable)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.state.authenticator = deps.auth_service
    app.state.auth_cookie_name = DEFAULT_COOKIE_NAME
    app.state.public_origin = configured_public_origin or None
    app.state.tool_manager = deps.tool_manager
    app.state.model_profile_service = deps.model_profile_service
    app.state.system_prompt_service = deps.system_prompt_service
    app.state.skill_service = deps.skill_service
    app.state.session_file_service = deps.session_file_service
    app.state.run_service = deps.run_service
    app.state.memory_service = deps.memory_service
    app.state.knowledge_service = deps.knowledge_service
    app.include_router(health_router)
    if deps.model_profile_service is not None:
        app.include_router(build_llm_admin_router(deps.model_profile_service))
    if deps.system_prompt_service is not None:
        app.include_router(build_system_prompt_admin_router(deps.system_prompt_service))
    if deps.skill_service is not None:
        app.include_router(build_skill_admin_router(deps.skill_service))
        app.include_router(build_skill_user_router(deps.skill_service))
    if deps.session_file_service is not None:
        app.include_router(build_session_file_router(deps.session_file_service))
    if deps.project_service is not None:
        app.include_router(build_project_router(deps.project_service))
    if deps.session_service is not None:
        app.include_router(build_session_router(deps.session_service))
    if deps.run_service is not None:
        app.include_router(build_run_router(deps.run_service, deps.run_stream_service))
    if deps.message_service is not None:
        app.include_router(build_message_router(deps.message_service))
    if deps.auth_service is not None:
        app.include_router(
            build_auth_router(
                deps.auth_service,
                cookie=AuthCookieConfig(
                    name=DEFAULT_COOKIE_NAME,
                    secure=auth_cookie_secure,
                ),
            )
        )
    if deps.tool_manager is not None:
        app.include_router(build_mcp_router(deps.tool_manager))
        app.include_router(build_tools_router(deps.tool_manager))
    if deps.knowledge_service is not None:
        app.state.knowledge_original_store = deps.knowledge_originals
        assert deps.document_validator is not None
        app.include_router(build_knowledge_router(deps.knowledge_service, deps.document_validator))
        if (
            deps.project_service is not None
            and deps.query_services is not None
            and deps.query_services.project_knowledge is not None
        ):
            app.include_router(
                build_project_knowledge_router(
                    deps.query_services.project_knowledge,
                    deps.project_service,
                )
            )
    return app


__all__ = ["create_app"]
