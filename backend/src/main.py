"""应用组装入口。"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from api.auth import AuthCookieConfig, build_auth_router
from api.auth_middleware import AuthPrecheckMiddleware
from api.file import build_session_file_router
from api.health import router as health_router
from api.knowledge import build_knowledge_router
from api.llm import build_llm_admin_router, build_llm_user_router
from api.mcp import build_mcp_router
from api.messages import build_message_router
from api.openapi import install_openapi_normalizer
from api.org import build_org_admin_router
from api.preference import build_preference_router
from api.project_knowledge import build_project_knowledge_router
from api.projects import build_project_router
from api.runs import build_run_router
from api.security import DEFAULT_COOKIE_NAME
from api.sessions import build_session_router
from api.skill import (
    build_skill_admin_router,
    build_skill_personal_router,
    build_skill_user_router,
)
from api.subagent import build_subagent_admin_router
from api.system_prompt import build_system_prompt_admin_router
from api.tools import build_tools_router
from api.upload_middleware import UploadAdmissionMiddleware
from app.auth.service import AuthService
from app.file import SessionFileService
from app.knowledge.service import KnowledgeService
from app.llm import ModelProfileService
from app.message.service import MessageService
from app.org import OrgService
from app.preference import PreferenceService
from app.project.service import ProjectService
from app.run.service import RunService
from app.session.service import SessionService
from app.skill import SkillService
from app.subagent import SubagentService
from app.system_prompt import SystemPromptService
from app.tool import ToolManager
from bootstrap.api import ApiDependencies, build_api_dependencies
from bootstrap.config import AuthSettings
from domain.file import FileStorageError

logger = logging.getLogger(__name__)


_DATABASE_BACKED_ROUTE_PREFIXES = (
    "/api/v1/auth",
    "/api/v1/projects",
    "/api/v1/sessions",
    "/api/v1/admin",
    "/api/v1/tools",
    "/api/v1/knowledge-bases",
    "/api/v1/me",
    "/api/v1/skills",
    "/api/v1/models",
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


async def _handle_file_storage_unavailable(request: Request, error: Exception) -> JSONResponse:
    """只映射专用对象存储异常，不吞掉其他 RuntimeError。"""
    del error
    logger.error("file_storage_unavailable path=%s", request.url.path)
    return JSONResponse(
        status_code=503,
        content={"detail": "file_storage_unavailable"},
    )


async def _handle_request_validation_error(request: Request, error: Exception) -> JSONResponse:
    """去除请求值，避免校验响应回显管理员提交的 API Key。"""
    del request
    if not isinstance(error, RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": "request_invalid"})
    safe_errors = [
        {key: value for key, value in item.items() if key != "input"} for item in error.errors()
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
    preference_service: PreferenceService | None = None,
    knowledge_service: KnowledgeService | None = None,
    skill_service: SkillService | None = None,
    session_file_service: SessionFileService | None = None,
    subagent_service: SubagentService | None = None,
    org_service: OrgService | None = None,
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
        preference_service=preference_service,
        knowledge_service=knowledge_service,
        skill_service=skill_service,
        session_file_service=session_file_service,
        subagent_service=subagent_service,
        org_service=org_service,
    )
    auth_settings = AuthSettings.from_environment()
    if auth_cookie_secure is None:
        auth_cookie_secure = auth_settings.cookie_secure
    configured_public_origin = (
        public_origin.strip() if public_origin is not None else (auth_settings.public_origin or "")
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
    # 统一规范化 OpenAPI 文档，消除 openapi_extra 残留局部 $defs 与悬空 #/$defs/ 引用。
    install_openapi_normalizer(app)
    # Starlette add_middleware 采用 LIFO 栈顺序：后注册的位于外层先执行。
    # 先挂载内层 UploadAdmissionMiddleware，再挂载外层 AuthPrecheckMiddleware，
    # 确保请求按 AuthPrecheckMiddleware -> UploadAdmissionMiddleware -> FastAPI 路由 依次处理。
    app.add_middleware(UploadAdmissionMiddleware)
    app.add_middleware(
        AuthPrecheckMiddleware,
        fastapi_app=app,
    )
    app.add_exception_handler(SQLAlchemyError, _handle_database_unavailable)
    app.add_exception_handler(OSError, _handle_os_error)
    app.add_exception_handler(FileStorageError, _handle_file_storage_unavailable)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    # 仅保留中间件与运行期/测试真正读取的 5 个应用级状态属性，避免挂接无人消费的冗余引用。
    app.state.authenticator = deps.auth_service
    app.state.auth_cookie_name = DEFAULT_COOKIE_NAME
    app.state.public_origin = configured_public_origin or None
    app.state.tool_manager = deps.tool_manager
    app.state.preference_service = deps.preference_service
    app.include_router(health_router)
    if deps.org_service is not None:
        app.include_router(build_org_admin_router(deps.org_service))
    if deps.model_profile_service is not None:
        app.include_router(build_llm_admin_router(deps.model_profile_service))
        app.include_router(build_llm_user_router(deps.model_profile_service))
    if deps.system_prompt_service is not None:
        app.include_router(build_system_prompt_admin_router(deps.system_prompt_service))
    if deps.preference_service is not None:
        app.include_router(build_preference_router(deps.preference_service))
    if deps.subagent_service is not None:
        app.include_router(build_subagent_admin_router(deps.subagent_service))
    if deps.skill_service is not None:
        app.include_router(build_skill_admin_router(deps.skill_service))
        app.include_router(build_skill_user_router(deps.skill_service))
        app.include_router(build_skill_personal_router(deps.skill_service))
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
