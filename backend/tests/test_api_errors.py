"""统一领域异常映射表、DomainErrorRoute 与 SafeMcpRoute 单元测试。"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from api.errors import DomainErrorRoute, domain_error_to_http
from api.knowledge import _KnowledgeErrorRoute
from api.mcp_errors import SafeMcpRoute, mcp_error_to_http
from api.upload import UploadTooLargeError
from app.file import FileInputError
from app.knowledge.service import (
    KnowledgeInputError,
    KnowledgeNotFoundError,
    KnowledgePermissionDeniedError,
    KnowledgeSearchUnavailableError,
)
from app.project.knowledge import KnowledgeNotFoundError as ProjectKnowledgeNotFoundError
from app.run.service import InvalidRunInputError
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
    OriginalStorageUnavailableError,
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
    SubagentInvalidSessionError,
    SubagentModelUnavailableError,
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
from infrastructure.feishu import FeishuError


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_detail"),
    (
        # Auth
        (InvalidEmailError(), 422, "invalid_email"),
        (InvalidPasswordError(), 422, "password_invalid"),
        (InvalidDisplayNameError(), 422, "display_name_invalid"),
        (EmailAlreadyRegisteredError(), 409, "email_exists"),
        (AuthProviderDisabledError("mock_not_enabled"), 409, "mock_not_enabled"),
        (InvalidCredentialsError(), 401, "invalid_credentials"),
        (InvalidCurrentPasswordError(), 400, "current_password_invalid"),
        (PasswordChangeNotSupportedError(), 400, "password_change_not_supported"),
        (InvalidReturnToError(), 400, "invalid_return_to"),
        (InvalidOAuthStateError(), 400, "invalid_oauth_state"),
        (UserDisabledError(), 403, "user_disabled"),
        (MockUserNotFoundError(), 404, "mock_user_not_found"),
        (InvalidSessionError(), 401, "not_authenticated"),
        (RateLimitExceededError(), 429, "rate_limited"),
        (RateLimitUnavailableError(), 503, "rate_limit_unavailable"),
        (OAuthStateUnavailableError(), 503, "oauth_state_unavailable"),
        (SsoAuthenticationError("upstream_failed"), 502, "feishu_auth_failed"),
        # Org & Feishu
        (OrgAuthorizationError("super_admin_required"), 403, "super_admin_required"),
        (OrgUserNotFoundError(), 404, "user_not_found"),
        (DepartmentNotFoundError(), 422, "department_not_found"),
        (InvalidDeptAdminScopeError(), 422, "dept_admin_departments_required"),
        (DepartmentCycleError(), 422, "department_cycle_detected"),
        (OrgSyncValidationError(), 422, "org_sync_validation_failed"),
        (LastSuperAdminProtectedError(), 409, "last_super_admin_protected"),
        (FeishuNotEnabledError(), 409, "feishu_not_enabled"),
        (FeishuError("secret token leak"), 502, "feishu_sync_failed"),
        # Access
        (AccessPermissionDeniedError("department_scope_denied"), 403, "department_scope_denied"),
        (
            InvalidVisibilityConfigError("visibility_grants_invalid"),
            422,
            "visibility_grants_invalid",
        ),
        # Project & Session
        (ProjectNotFoundError(), 404, "project_not_found"),
        (InvalidProjectNameError(), 422, "project_name_invalid"),
        (
            InvalidProjectInputError("project_instructions_too_long"),
            422,
            "project_instructions_too_long",
        ),
        (ProjectHasSessionsError(), 409, "project_has_sessions"),
        (SessionNotFoundError(), 404, "session_not_found"),
        (MessageSessionNotFoundError("session_not_found"), 404, "session_not_found"),
        (InvalidSessionTitleError(), 422, "session_title_invalid"),
        (InvalidSessionUpdateError(), 422, "session_update_invalid"),
        (InvalidSessionFilterError(), 422, "invalid_session_filter"),
        # Run（验证子类优先于 RunConflictError）
        (RunKnowledgeBaseNotFoundError(), 404, "knowledge_base_not_found"),
        (RunNotFoundError(), 404, "run_not_found"),
        (ToolRetryConfirmationRequiredError(), 409, "tool_retry_confirmation_required"),
        (SessionHasActiveRunError(), 409, "session_has_active_run"),
        (RunConflictError("run_active_conflict"), 409, "run_active_conflict"),
        (RunStateError(""), 409, "run_conflict"),
        (InvalidRunInputError("run_text_empty"), 422, "run_text_empty"),
        (RunInputResourceError("run_skill_unavailable"), 422, "run_skill_unavailable"),
        # Preference
        (UserMemoryNotFoundError(), 404, "user_memory_not_found"),
        (UserMemoryLimitReachedError(), 409, "user_memory_limit_reached"),
        (InvalidCustomInstructionsError(), 422, "custom_instructions_invalid"),
        (InvalidUserMemoryContentError(), 422, "user_memory_content_invalid"),
        (PreferenceError("user_not_found"), 401, "user_not_found"),
        (PreferenceError("preference_invalid"), 422, "preference_invalid"),
        # LLM
        (ModelProfileAuthorizationError(), 403, "admin_required"),
        (ModelProfileNotFoundError(), 404, "llm_model_profile_not_found"),
        (
            ModelProfileConflictError("llm_model_profile_name_conflict"),
            409,
            "llm_model_profile_name_conflict",
        ),
        (ModelProfileError("llm_model_profile_invalid"), 422, "llm_model_profile_invalid"),
        # System Prompt
        (SystemPromptAuthorizationError("admin_required"), 403, "admin_required"),
        (SystemPromptNotFoundError(), 404, "system_prompt_not_found"),
        (SystemPromptConflictError(), 409, "system_prompt_version_conflict"),
        (SystemPromptUnavailableError(), 503, "system_prompt_unavailable"),
        (SystemPromptError(), 422, "system_prompt_invalid"),
        # Subagent
        (SubagentInvalidSessionError(), 401, "not_authenticated"),
        (SubagentAdminRequiredError(), 403, "admin_required"),
        (SubagentNotFoundError(), 404, "subagent_not_found"),
        (SubagentSlugConflictError(), 409, "subagent_slug_conflict"),
        (SubagentVersionConflictError(), 409, "subagent_version_conflict"),
        (SubagentSlugReservedError(), 422, "subagent_slug_reserved"),
        (SubagentToolForbiddenError(), 422, "subagent_tool_forbidden"),
        (SubagentModelUnavailableError(), 422, "subagent_model_unavailable"),
        # MCP（验证 McpDiscoveryTimeoutError 先于 McpDiscoveryError）
        (McpInvalidSessionError(), 401, "not_authenticated"),
        (McpAdminRequiredError(), 403, "admin_required"),
        (McpAuthorizationChangedError(), 403, "admin_required"),
        (McpServerNotFoundError(), 404, "resource_not_found"),
        (McpServerNameConflictError(), 409, "mcp_conflict"),
        (McpConfigVersionConflictError(), 409, "mcp_conflict"),
        (McpToolConflictError(), 409, "mcp_conflict"),
        (McpDiscoveryTimeoutError(), 504, "mcp_discovery_timeout"),
        (McpDiscoveryError(), 502, "mcp_discovery_failed"),
        (McpCatalogPersistenceError(), 503, "mcp_unavailable"),
        (McpSecretError(), 503, "mcp_unavailable"),
        # Skill
        (SkillAuthorizationError(), 403, "admin_required"),
        (SkillAuthorizationError("department_admin_denied"), 403, "department_admin_denied"),
        (SkillUserAuthorizationError(), 401, "not_authenticated"),
        (SkillNotFoundError("skill_not_found"), 404, "skill_not_found"),
        (SkillConflictError("skill_revision_conflict"), 409, "skill_revision_conflict"),
        (
            SkillDependencyUnsatisfiedError("skill_dependency_missing"),
            422,
            "skill_dependency_missing",
        ),
        (
            SkillPackageError("skill_dependency_environment_unavailable"),
            503,
            "skill_dependency_environment_unavailable",
        ),
        (SkillPackageError("skill_zip_invalid"), 422, "skill_zip_invalid"),
        (
            SkillPackageError("skill_zip_path_invalid", member_path="scripts/bad.py"),
            422,
            {"code": "skill_zip_path_invalid", "member_path": "scripts/bad.py"},
        ),
        (PersonalSkillError("skill_markdown_invalid"), 422, "skill_markdown_invalid"),
        (
            PersonalSkillError("skill_markdown_field_unsupported", field="scripts"),
            422,
            {"code": "skill_markdown_field_unsupported", "field": "scripts"},
        ),
        # 字段名含不可打印控制字符时丢弃 field，仅保留稳定错误码。
        (
            PersonalSkillError("skill_markdown_invalid", field="bad\nfield"),
            422,
            "skill_markdown_invalid",
        ),
        # Knowledge & Project-Knowledge
        (KnowledgeNotFoundError("document_not_found"), 404, "document_not_found"),
        (ProjectKnowledgeNotFoundError("ignored"), 404, "knowledge_base_not_found"),
        (KnowledgePermissionDeniedError(""), 403, "knowledge_permission_denied"),
        (KnowledgePermissionDeniedError("department_write_denied"), 403, "department_write_denied"),
        (KnowledgeSearchUnavailableError("boom"), 503, "knowledge_search_unavailable"),
        (OriginalStorageUnavailableError(), 503, "knowledge_storage_unavailable"),
        (ProjectKnowledgeLimitReachedError(), 409, "project_knowledge_limit_reached"),
        (KnowledgeInputError("document_update_in_progress"), 409, "document_update_in_progress"),
        (KnowledgeInputError("idempotency_conflict"), 409, "idempotency_conflict"),
        (
            KnowledgeInputError("knowledge_file_type_unsupported"),
            415,
            "knowledge_file_type_unsupported",
        ),
        (
            KnowledgeInputError("knowledge_archive_expanded_size_limit"),
            413,
            "knowledge_archive_expanded_size_limit",
        ),
        (
            KnowledgeInputError("knowledge_storage_quota_exceeded"),
            429,
            "knowledge_storage_quota_exceeded",
        ),
        (KnowledgeInputError("document_title_invalid"), 422, "document_title_invalid"),
        # File & Upload
        (FileStorageError("file_storage_not_found"), 503, "file_storage_unavailable"),
        (FileInputError("session_file_not_found"), 404, "session_file_not_found"),
        (FileInputError("idempotency_conflict"), 409, "idempotency_conflict"),
        (FileInputError("file_type_unsupported"), 422, "file_type_unsupported"),
        (TextFileReadError("file_not_text"), 415, "file_not_text"),
        (TextFileReadError("file_offset_invalid"), 422, "file_offset_invalid"),
        (UploadTooLargeError("upload_too_large"), 413, "document_file_too_large"),
        (UploadTooLargeError("skill_zip_too_large"), 413, "skill_zip_too_large"),
        (UploadTooLargeError("skill_markdown_too_large"), 413, "skill_markdown_too_large"),
    ),
)
def test_domain_error_to_http_mapping(
    error: Exception,
    expected_status: int,
    expected_detail: str | dict[str, str],
) -> None:
    """逐项核对已注册领域异常的 HTTP 状态码与稳定 detail。"""
    mapped = domain_error_to_http(error)
    assert isinstance(mapped, HTTPException)
    assert mapped.status_code == expected_status
    assert mapped.detail == expected_detail


def test_domain_error_to_http_ignores_unregistered_exceptions() -> None:
    """未注册通用异常返回 None，不吞掉未预期故障或原始数据库 OSError。"""
    assert domain_error_to_http(RuntimeError("unexpected")) is None
    assert domain_error_to_http(ValueError("raw_value_error")) is None
    assert domain_error_to_http(ConnectionRefusedError("db_down")) is None
    assert domain_error_to_http(HTTPException(status_code=418, detail="teapot")) is None


def test_domain_error_route_works_on_bare_fastapi_without_create_app() -> None:
    """裸 FastAPI() 挂载 DomainErrorRoute 时自动生效，并保持未注册异常 500 透传。"""
    router = APIRouter(route_class=DomainErrorRoute)

    @router.get("/structured-skill")
    async def structured_skill() -> None:
        raise SkillPackageError("skill_zip_path_invalid", member_path="scripts/run.py")

    @router.get("/feishu")
    async def feishu_fail() -> None:
        raise FeishuError("bearer_secret_should_not_leak")

    @router.get("/unregistered")
    async def unregistered() -> None:
        raise RuntimeError("boom")

    app = FastAPI()
    app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as client:
        skill_resp = client.get("/structured-skill")
        assert skill_resp.status_code == 422
        assert skill_resp.json() == {
            "detail": {"code": "skill_zip_path_invalid", "member_path": "scripts/run.py"}
        }

        feishu_resp = client.get("/feishu")
        assert feishu_resp.status_code == 502
        assert feishu_resp.json() == {"detail": "feishu_sync_failed"}
        assert "bearer_secret" not in feishu_resp.text

        boom_resp = client.get("/unregistered")
        assert boom_resp.status_code == 500


def test_safe_mcp_route_masks_validation_errors_and_reuses_domain_mapping() -> None:
    """SafeMcpRoute 继承 DomainErrorRoute，同时保持 422 不回显凭据与裸 ValueError 脱敏。"""

    class _SecretPayload(BaseModel):
        token: str
        count: int

    router = APIRouter(route_class=SafeMcpRoute)

    @router.post("/secret")
    async def accept_secret(payload: _SecretPayload) -> dict[str, object]:
        return {"count": payload.count}

    @router.get("/missing")
    async def missing_server() -> None:
        raise McpServerNotFoundError()

    @router.get("/bad-value")
    async def bad_value() -> None:
        raise ValueError("secret-dsn://credentials")

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        invalid_resp = client.post("/secret", json={"token": "super-secret-key", "count": "bad"})
        assert invalid_resp.status_code == 422
        assert invalid_resp.json() == {"detail": "mcp_request_invalid"}
        assert "super-secret-key" not in invalid_resp.text

        missing_resp = client.get("/missing")
        assert missing_resp.status_code == 404
        assert missing_resp.json() == {"detail": "resource_not_found"}

        value_resp = client.get("/bad-value")
        assert value_resp.status_code == 422
        assert value_resp.json() == {"detail": "mcp_request_invalid"}
        assert "credentials" not in value_resp.text

    # 兼容直接调用 mcp_error_to_http 的辅助函数语义。
    assert mcp_error_to_http(McpDiscoveryTimeoutError()).status_code == 504
    assert mcp_error_to_http(ValueError("bad")).detail == "mcp_request_invalid"
    with pytest.raises(RuntimeError, match="unhandled"):
        mcp_error_to_http(RuntimeError("unhandled"))


def test_knowledge_error_route_maps_exact_lookup_error_but_not_key_or_index_error() -> None:
    """_KnowledgeErrorRoute 仅映射精确裸 LookupError 为 404，不吞掉 KeyError / IndexError。"""
    router = APIRouter(route_class=_KnowledgeErrorRoute)

    @router.get("/bare-lookup")
    async def bare_lookup() -> None:
        raise LookupError("knowledge_base_not_found")

    @router.get("/key-error")
    async def key_error() -> None:
        raise KeyError("missing_internal_key")

    @router.get("/index-error")
    async def index_error() -> None:
        raise IndexError("list index out of range")

    app = FastAPI()
    app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as client:
        lookup_resp = client.get("/bare-lookup")
        assert lookup_resp.status_code == 404
        assert lookup_resp.json() == {"detail": "knowledge_base_not_found"}

        key_resp = client.get("/key-error")
        assert key_resp.status_code == 500

        index_resp = client.get("/index-error")
        assert index_resp.status_code == 500
