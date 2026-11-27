"""统一 FastAPI 领域异常映射表与 APIRoute 包装层。

采用 APIRoute 子类（DomainErrorRoute）而非仅注册在 create_app() 的全局 exception_handler，
确保无论通过 create_app() 还是单测直接构造裸 FastAPI() 挂载单个路由器时，
所有领域异常都能 100% 映射为约定的 HTTP 状态码与稳定错误码。
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

from api.security import NOT_AUTHENTICATED
from api.upload import UploadTooLargeError
from app.file import FileInputError
from app.knowledge.service import (
    KnowledgeInputError,
    KnowledgeNotFoundError,
    KnowledgePermissionDeniedError,
    KnowledgeSearchUnavailableError,
)
from app.project.knowledge import KnowledgeNotFoundError as ProjectKnowledgeNotFoundError
from app.skill.personal import PersonalSkillError
from app.skill.service import SkillAuthorizationError, SkillUserAuthorizationError
from domain.access import AccessPermissionDeniedError, InvalidVisibilityConfigError
from domain.auth import (
    AuthProviderDisabledError,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidCurrentPasswordError,
    InvalidDisplayNameError,
    InvalidEmailError,
    InvalidOAuthStateError,
    InvalidPasswordError,
    InvalidReturnToError,
    InvalidSessionError,
    MockUserNotFoundError,
    OAuthStateUnavailableError,
    PasswordChangeNotSupportedError,
    RateLimitExceededError,
    RateLimitUnavailableError,
    SsoAuthenticationError,
    UserDisabledError,
)
from domain.file import FileStorageError, TextFileReadError
from domain.knowledge.model import (
    OriginalStorageError,
    ProjectKnowledgeLimitReachedError,
)
from domain.llm.model.profile_errors import (
    ModelProfileAuthorizationError,
    ModelProfileConflictError,
    ModelProfileError,
    ModelProfileNotFoundError,
)
from domain.mcp import (
    McpAdminRequiredError,
    McpAuthorizationChangedError,
    McpCatalogPersistenceError,
    McpConfigVersionConflictError,
    McpDiscoveryError,
    McpDiscoveryTimeoutError,
    McpInvalidSessionError,
    McpSecretError,
    McpServerNameConflictError,
    McpServerNotFoundError,
    McpToolConflictError,
)
from domain.message import SessionNotFoundError as MessageSessionNotFoundError
from domain.org import (
    DepartmentCycleError,
    DepartmentNotFoundError,
    FeishuNotEnabledError,
    InvalidDeptAdminScopeError,
    LastSuperAdminProtectedError,
    OrgAuthorizationError,
    OrgSyncValidationError,
    OrgUserNotFoundError,
)
from domain.preference import (
    InvalidCustomInstructionsError,
    InvalidUserMemoryContentError,
    PreferenceError,
    UserMemoryLimitReachedError,
    UserMemoryNotFoundError,
)
from domain.project import (
    InvalidProjectInputError,
    InvalidProjectNameError,
    ProjectHasSessionsError,
    ProjectNotFoundError,
)
from domain.run.model.errors import (
    InvalidRunInputError,
    RunConflictError,
    RunInputResourceError,
    RunKnowledgeBaseNotFoundError,
    RunNotFoundError,
    RunStateError,
    SessionHasActiveRunError,
    ToolRetryConfirmationRequiredError,
)
from domain.session import (
    InvalidSessionFilterError,
    InvalidSessionTitleError,
    InvalidSessionUpdateError,
    SessionNotFoundError,
)
from domain.skill import (
    SkillConflictError,
    SkillDependencyUnsatisfiedError,
    SkillNotFoundError,
    SkillPackageError,
)
from domain.subagent import (
    SubagentAdminRequiredError,
    SubagentError,
    SubagentInvalidSessionError,
    SubagentNotFoundError,
    SubagentSlugConflictError,
    SubagentSlugReservedError,
    SubagentToolForbiddenError,
    SubagentVersionConflictError,
)
from domain.system_prompt import (
    SystemPromptAuthorizationError,
    SystemPromptConflictError,
    SystemPromptError,
    SystemPromptNotFoundError,
    SystemPromptUnavailableError,
)

# 静态一对一映射表：(异常类型或元组, HTTP 状态码, 固定 detail 或 detail 提取函数)。
# 匹配按列表顺序进行，子类异常务必排在父类之前。
type _DetailSpec = str | Callable[[Exception], str | dict[str, str]]
type _ErrorRule = tuple[type[Exception] | tuple[type[Exception], ...], int, _DetailSpec]

_KNOWLEDGE_CONFLICT_CODES = frozenset({"document_update_in_progress", "idempotency_conflict"})
_KNOWLEDGE_QUOTA_CODES = frozenset(
    {"knowledge_storage_quota_exceeded", "knowledge_document_quota_exceeded"}
)
_KNOWLEDGE_UNSUPPORTED_MEDIA_CODES = frozenset(
    {
        "document_type_unsupported",
        "knowledge_file_type_unsupported",
        "knowledge_legacy_office_unsupported",
    }
)
_KNOWLEDGE_TOO_LARGE_CODES = frozenset(
    {
        "knowledge_file_size_invalid",
        "knowledge_archive_expanded_size_limit",
        "knowledge_extracted_text_limit",
    }
)


def _error_code(error: Exception) -> str:
    """读取领域异常携带的稳定 error.code。"""
    return str(getattr(error, "code", str(error)))


def _first_arg_or(default: str) -> Callable[[Exception], str]:
    """提取异常首个参数，缺省时回退到安全默认错误码。"""

    def _extract(error: Exception) -> str:
        return str(error.args[0]) if error.args else default

    return _extract


def _str_or(default: str) -> Callable[[Exception], str]:
    """读取非空异常字符串，空串时回退到默认错误码。"""

    def _extract(error: Exception) -> str:
        return str(error) or default

    return _extract


def _personal_skill_detail(error: Exception) -> str | dict[str, str]:
    """为个人 Skill 校验异常返回白名单稳定错误码，并在字段名合法时附带表单定位。"""
    explicit = getattr(error, "code", None)
    if isinstance(explicit, str):
        code = explicit
    else:
        value = str(error)
        code = (
            value
            if value.replace("_", "").isalnum() and value.isascii()
            else "skill_personal_content_invalid"
        )
    field = getattr(error, "field", None)
    if (
        isinstance(field, str)
        and 0 < len(field) <= 128
        and all(character.isprintable() for character in field)
    ):
        # 仅回传可打印短字段名供前端表单高亮，绝不回显 Markdown 或 YAML 原始内容。
        return {"code": code, "field": field}
    return code


def _skill_package_http(error: SkillPackageError) -> HTTPException:
    """将 Skill 依赖环境不可用映射为 503，其余包/配置校验错误映射为 422（保留安全 member_path）。"""
    if error.code == "skill_dependency_environment_unavailable":
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=error.code)
    detail: str | dict[str, str] = (
        error.code
        if error.member_path is None
        else {"code": error.code, "member_path": error.member_path}
    )
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _knowledge_input_http(error: KnowledgeInputError) -> HTTPException:
    """按知识库业务错误码细分 409/413/415/429/422 状态码。"""
    code = str(error)
    if code in _KNOWLEDGE_CONFLICT_CODES:
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=code)
    if code in _KNOWLEDGE_UNSUPPORTED_MEDIA_CODES:
        return HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=code)
    if code in _KNOWLEDGE_TOO_LARGE_CODES:
        return HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=code)
    if code in _KNOWLEDGE_QUOTA_CODES:
        return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=code)
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=code)


def _file_input_http(error: FileInputError) -> HTTPException:
    """将会话文件输入错误按稳定错误码映射为 404/409/422。"""
    if error.code == "session_file_not_found":
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error.code)
    if error.code == "idempotency_conflict":
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error.code)
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=error.code)


def _text_file_read_http(error: TextFileReadError) -> HTTPException:
    """将文本预览错误映射为 415（非文本）或 422（偏移/内容异常）。"""
    if error.code == "file_not_text":
        return HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=error.code)
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=error.code)


def _preference_http(error: PreferenceError) -> HTTPException:
    """将偏好域基础异常中失效用户映射为 401，其余校验失败映射为 422。"""
    status_code = (
        status.HTTP_401_UNAUTHORIZED
        if error.code == "user_not_found"
        else status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    return HTTPException(status_code=status_code, detail=error.code)


def _upload_too_large_detail(error: Exception) -> str:
    """上传超限错误码：缺省或通用 upload_too_large 兜底为 document_file_too_large。"""
    code = str(error)
    return "document_file_too_large" if not code or code == "upload_too_large" else code


# 统一业务域异常映射表；按领域分组，且子类规则严格先于父类规则。
_STATIC_ERROR_RULES: tuple[_ErrorRule, ...] = (
    # --- 统一权限复核（替代 llm / skill / system_prompt / org / mcp / subagent 重复逻辑）---
    (
        (
            ModelProfileAuthorizationError,
            McpAdminRequiredError,
            McpAuthorizationChangedError,
            SubagentAdminRequiredError,
        ),
        status.HTTP_403_FORBIDDEN,
        "admin_required",
    ),
    (SkillAuthorizationError, status.HTTP_403_FORBIDDEN, _first_arg_or("admin_required")),
    (SkillUserAuthorizationError, status.HTTP_401_UNAUTHORIZED, _first_arg_or(NOT_AUTHENTICATED)),
    (
        (SystemPromptAuthorizationError, OrgAuthorizationError, AccessPermissionDeniedError),
        status.HTTP_403_FORBIDDEN,
        _error_code,
    ),
    (
        (McpInvalidSessionError, SubagentInvalidSessionError, InvalidSessionError),
        status.HTTP_401_UNAUTHORIZED,
        NOT_AUTHENTICATED,
    ),
    (InvalidVisibilityConfigError, status.HTTP_422_UNPROCESSABLE_CONTENT, _error_code),
    # --- Auth ---
    (InvalidEmailError, status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_email"),
    (InvalidPasswordError, status.HTTP_422_UNPROCESSABLE_CONTENT, "password_invalid"),
    (InvalidDisplayNameError, status.HTTP_422_UNPROCESSABLE_CONTENT, "display_name_invalid"),
    (EmailAlreadyRegisteredError, status.HTTP_409_CONFLICT, "email_exists"),
    (AuthProviderDisabledError, status.HTTP_409_CONFLICT, str),
    (InvalidCredentialsError, status.HTTP_401_UNAUTHORIZED, "invalid_credentials"),
    (InvalidCurrentPasswordError, status.HTTP_400_BAD_REQUEST, "current_password_invalid"),
    (
        PasswordChangeNotSupportedError,
        status.HTTP_400_BAD_REQUEST,
        "password_change_not_supported",
    ),
    (InvalidReturnToError, status.HTTP_400_BAD_REQUEST, "invalid_return_to"),
    (InvalidOAuthStateError, status.HTTP_400_BAD_REQUEST, "invalid_oauth_state"),
    (UserDisabledError, status.HTTP_403_FORBIDDEN, "user_disabled"),
    (MockUserNotFoundError, status.HTTP_404_NOT_FOUND, "mock_user_not_found"),
    (RateLimitExceededError, status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited"),
    (RateLimitUnavailableError, status.HTTP_503_SERVICE_UNAVAILABLE, "rate_limit_unavailable"),
    (OAuthStateUnavailableError, status.HTTP_503_SERVICE_UNAVAILABLE, "oauth_state_unavailable"),
    (SsoAuthenticationError, status.HTTP_502_BAD_GATEWAY, "feishu_auth_failed"),
    # --- Org & Feishu ---
    (OrgUserNotFoundError, status.HTTP_404_NOT_FOUND, _error_code),
    (
        (
            DepartmentNotFoundError,
            InvalidDeptAdminScopeError,
            DepartmentCycleError,
            OrgSyncValidationError,
        ),
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        _error_code,
    ),
    ((LastSuperAdminProtectedError, FeishuNotEnabledError), status.HTTP_409_CONFLICT, _error_code),
    # --- Project ---
    (ProjectNotFoundError, status.HTTP_404_NOT_FOUND, "project_not_found"),
    (InvalidProjectNameError, status.HTTP_422_UNPROCESSABLE_CONTENT, "project_name_invalid"),
    (InvalidProjectInputError, status.HTTP_422_UNPROCESSABLE_CONTENT, str),
    (ProjectHasSessionsError, status.HTTP_409_CONFLICT, "project_has_sessions"),
    # --- Session & Message（两域各自定义独立 SessionNotFoundError 类）---
    (
        (SessionNotFoundError, MessageSessionNotFoundError),
        status.HTTP_404_NOT_FOUND,
        "session_not_found",
    ),
    (InvalidSessionTitleError, status.HTTP_422_UNPROCESSABLE_CONTENT, "session_title_invalid"),
    (InvalidSessionUpdateError, status.HTTP_422_UNPROCESSABLE_CONTENT, "session_update_invalid"),
    (InvalidSessionFilterError, status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_session_filter"),
    # --- Run（ToolRetryConfirmationRequiredError / SessionHasActiveRunError 先于父类）---
    (RunKnowledgeBaseNotFoundError, status.HTTP_404_NOT_FOUND, "knowledge_base_not_found"),
    (RunNotFoundError, status.HTTP_404_NOT_FOUND, "run_not_found"),
    (
        ToolRetryConfirmationRequiredError,
        status.HTTP_409_CONFLICT,
        "tool_retry_confirmation_required",
    ),
    (SessionHasActiveRunError, status.HTTP_409_CONFLICT, "session_has_active_run"),
    ((RunConflictError, RunStateError), status.HTTP_409_CONFLICT, _str_or("run_conflict")),
    ((InvalidRunInputError, RunInputResourceError), status.HTTP_422_UNPROCESSABLE_CONTENT, str),
    # --- Preference（子类先于 PreferenceError）---
    (UserMemoryNotFoundError, status.HTTP_404_NOT_FOUND, _error_code),
    (UserMemoryLimitReachedError, status.HTTP_409_CONFLICT, _error_code),
    (
        (InvalidCustomInstructionsError, InvalidUserMemoryContentError),
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        _error_code,
    ),
    # --- LLM（子类先于 ModelProfileError）---
    (ModelProfileNotFoundError, status.HTTP_404_NOT_FOUND, _error_code),
    (ModelProfileConflictError, status.HTTP_409_CONFLICT, _error_code),
    (ModelProfileError, status.HTTP_422_UNPROCESSABLE_CONTENT, _error_code),
    # --- System Prompt（子类先于 SystemPromptError）---
    (SystemPromptNotFoundError, status.HTTP_404_NOT_FOUND, _error_code),
    (SystemPromptConflictError, status.HTTP_409_CONFLICT, _error_code),
    (SystemPromptUnavailableError, status.HTTP_503_SERVICE_UNAVAILABLE, _error_code),
    (SystemPromptError, status.HTTP_422_UNPROCESSABLE_CONTENT, _error_code),
    # --- Subagent（子类先于 SubagentError）---
    (SubagentNotFoundError, status.HTTP_404_NOT_FOUND, _error_code),
    (
        (SubagentSlugConflictError, SubagentVersionConflictError),
        status.HTTP_409_CONFLICT,
        _error_code,
    ),
    (
        (SubagentSlugReservedError, SubagentToolForbiddenError, SubagentError),
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        _error_code,
    ),
    # --- MCP（McpDiscoveryTimeoutError 先于 McpDiscoveryError）---
    (McpServerNotFoundError, status.HTTP_404_NOT_FOUND, "resource_not_found"),
    (
        (McpServerNameConflictError, McpConfigVersionConflictError, McpToolConflictError),
        status.HTTP_409_CONFLICT,
        "mcp_conflict",
    ),
    (McpDiscoveryTimeoutError, status.HTTP_504_GATEWAY_TIMEOUT, "mcp_discovery_timeout"),
    (McpDiscoveryError, status.HTTP_502_BAD_GATEWAY, "mcp_discovery_failed"),
    (
        (McpCatalogPersistenceError, McpSecretError),
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "mcp_unavailable",
    ),
    # --- Skill ---
    (SkillNotFoundError, status.HTTP_404_NOT_FOUND, _error_code),
    (SkillConflictError, status.HTTP_409_CONFLICT, _error_code),
    (SkillDependencyUnsatisfiedError, status.HTTP_422_UNPROCESSABLE_CONTENT, _error_code),
    (PersonalSkillError, status.HTTP_422_UNPROCESSABLE_CONTENT, _personal_skill_detail),
    # --- Knowledge & Project-Knowledge ---
    (KnowledgeNotFoundError, status.HTTP_404_NOT_FOUND, str),
    (ProjectKnowledgeNotFoundError, status.HTTP_404_NOT_FOUND, "knowledge_base_not_found"),
    (
        KnowledgePermissionDeniedError,
        status.HTTP_403_FORBIDDEN,
        _str_or("knowledge_permission_denied"),
    ),
    (
        KnowledgeSearchUnavailableError,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "knowledge_search_unavailable",
    ),
    (OriginalStorageError, status.HTTP_503_SERVICE_UNAVAILABLE, "knowledge_storage_unavailable"),
    (
        ProjectKnowledgeLimitReachedError,
        status.HTTP_409_CONFLICT,
        "project_knowledge_limit_reached",
    ),
    # --- File & Upload ---
    (FileStorageError, status.HTTP_503_SERVICE_UNAVAILABLE, "file_storage_unavailable"),
    (UploadTooLargeError, status.HTTP_413_CONTENT_TOO_LARGE, _upload_too_large_detail),
)


def _is_feishu_error(error: Exception) -> bool:
    """延迟判定飞书外部传输异常，避免 api.errors 在模块导入期拉起 infrastructure.feishu。"""
    feishu_module = sys.modules.get("infrastructure.feishu.client")
    if feishu_module is None:
        return False
    feishu_error_type = getattr(feishu_module, "FeishuError", None)
    return isinstance(feishu_error_type, type) and isinstance(error, feishu_error_type)


def domain_error_to_http(error: Exception) -> HTTPException | None:
    """将已注册领域异常转换为等价 HTTPException；未注册异常返回 None 交由上层处理。"""
    # 需依据错误码动态决策状态码或结构化 detail 的少量领域异常优先分派。
    if isinstance(error, SkillPackageError):
        return _skill_package_http(error)
    if isinstance(error, KnowledgeInputError):
        return _knowledge_input_http(error)
    if isinstance(error, FileInputError):
        return _file_input_http(error)
    if isinstance(error, TextFileReadError):
        return _text_file_read_http(error)

    for error_types, status_code, detail_spec in _STATIC_ERROR_RULES:
        if isinstance(error, error_types):
            detail = detail_spec(error) if callable(detail_spec) else detail_spec
            return HTTPException(status_code=status_code, detail=detail)

    if _is_feishu_error(error):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="feishu_sync_failed",
        )
    if isinstance(error, PreferenceError):
        return _preference_http(error)
    return None


class DomainErrorRoute(APIRoute):
    """在路由外层统一捕获并映射已注册领域异常，兼容 create_app() 与裸 FastAPI() 挂载。"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def domain_error_handler(request: Request) -> Response:
            try:
                return await original_handler(request)
            except Exception as error:
                mapped = domain_error_to_http(error)
                if mapped is None:
                    raise
                # 飞书外部传输异常截断底层异常链，避免意外暴露上游细节。
                if _is_feishu_error(error):
                    raise mapped from None
                raise mapped from error

        return domain_error_handler


class SafeMcpRoute(DomainErrorRoute):
    """在统一领域异常转换外层阻止 422 校验响应回显 MCP 凭据或连接配置。"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await original_handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={"detail": "mcp_request_invalid"},
                )
            except (ValueError, TypeError) as error:
                # 兜底映射 MCP 路径上的普通参数类型/值校验异常，不回显原始文本。
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="mcp_request_invalid",
                ) from error

        return safe_handler


__all__ = [
    "DomainErrorRoute",
    "SafeMcpRoute",
    "domain_error_to_http",
]
