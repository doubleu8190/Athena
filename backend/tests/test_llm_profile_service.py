"""模型版本管理员服务边界测试。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.llm.profile_service import ModelProfileService
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.llm.model.compression_policy import (
    MAX_COMPRESSION_TOKEN_BUDGET,
    CompressionPolicyInput,
    CompressionPolicyVersion,
)
from domain.llm.model.profile import ModelProfileInput, ModelProfileVersion, RuntimeModelProfile
from domain.llm.model.profile_errors import ModelProfileAuthorizationError, ModelProfileError


class _AuthReader:
    """提供测试用的当前账号状态。"""

    def __init__(self, user: User | None) -> None:
        self.user = user

    async def get_current_session_user(
        self, user_id: str, session_id: str, now: datetime
    ) -> User | None:
        return self.user


class _ProfileRepository:
    """记录可通过服务校验的配置创建。"""

    def __init__(self) -> None:
        self.created: list[ModelProfileInput] = []

    async def create(self, actor: AuthContext, value: ModelProfileInput) -> ModelProfileVersion:
        self.created.append(value)
        timestamp = datetime.now(UTC)
        return ModelProfileVersion(
            id="llmprof_test",
            name=value.name,
            protocol="chat_completions",
            base_url=value.base_url,
            model=value.model,
            has_api_key=bool(value.api_key),
            context_window_tokens=value.context_window_tokens,
            tokenizer_encoding=value.tokenizer_encoding,
            supports_stream=value.supports_stream,
            supports_tools=value.supports_tools,
            max_output_tokens=value.max_output_tokens,
            request_timeout_seconds=value.request_timeout_seconds,
            execution_timeout_seconds=value.execution_timeout_seconds,
            max_concurrency=value.max_concurrency,
            temperature=value.temperature,
            enabled=True,
            created_at=timestamp,
            supports_parallel_tool_calls=value.supports_parallel_tool_calls,
        )

    async def list(self, actor: AuthContext) -> tuple[ModelProfileVersion, ...]:
        return ()

    async def list_with_default(
        self, actor: AuthContext
    ) -> tuple[tuple[ModelProfileVersion, ...], str | None]:
        return await self.list(actor), await self.get_default_id(actor)

    async def set_enabled(
        self, actor: AuthContext, profile_id: str, enabled: bool
    ) -> ModelProfileVersion:
        raise AssertionError("test_repository_set_enabled_not_configured")

    async def set_default(self, actor: AuthContext, profile_id: str) -> ModelProfileVersion:
        raise AssertionError("test_repository_set_default_not_configured")

    async def get_default_runtime(self) -> RuntimeModelProfile | None:
        return None

    async def get_default_id(self, actor: AuthContext) -> str | None:
        return None

    async def get_runtime(self, profile_id: str) -> RuntimeModelProfile | None:
        return None

    async def get_current_compression_policy(self) -> CompressionPolicyVersion | None:
        return None

    async def get_compression_policy(self, policy_id: str) -> CompressionPolicyVersion | None:
        del policy_id
        return None

    async def set_compression_policy(
        self, actor: AuthContext, value: CompressionPolicyInput
    ) -> CompressionPolicyVersion:
        del actor, value
        raise AssertionError("test_repository_compression_policy_not_configured")


class _CompressionPolicyRepository(_ProfileRepository):
    """记录通过用例校验的不可变压缩策略。"""

    def __init__(self) -> None:
        super().__init__()
        self.policy_values: list[CompressionPolicyInput] = []

    async def get_current_compression_policy(self) -> CompressionPolicyVersion | None:
        return None

    async def get_compression_policy(self, policy_id: str) -> CompressionPolicyVersion | None:
        return None

    async def set_compression_policy(
        self,
        actor_context: AuthContext,
        value: CompressionPolicyInput,
    ) -> CompressionPolicyVersion:
        self.policy_values.append(value)
        return CompressionPolicyVersion(
            id="cmpol_service_test",
            compression_model_profile_id=value.compression_model_profile_id,
            trigger_tokens=value.trigger_tokens,
            recent_ratio=value.recent_ratio,
            recent_token_cap=value.recent_token_cap,
            summary_ratio=value.summary_ratio,
            summary_token_cap=value.summary_token_cap,
            prompt_template_version="context-summary-v1",
            created_at=datetime.now(UTC),
        )


def actor(role: UserRole = UserRole.SUPER_ADMIN) -> AuthContext:
    """创建测试身份。"""
    timestamp = datetime.now(UTC)
    return AuthContext(
        user=User(
            id="usr_test",
            email="admin@example.com",
            password_hash="unused",
            display_name=None,
            status=UserStatus.ACTIVE,
            created_at=timestamp,
            updated_at=timestamp,
            role=role,
        ),
        session_id="auth_test",
    )


def profile_input(api_key: str = "test-secret") -> ModelProfileInput:
    """创建合法配置字段。"""
    return ModelProfileInput(
        name="Test model",
        base_url="https://llm.example/v1",
        model="test-model",
        api_key=api_key,
        context_window_tokens=8192,
        max_output_tokens=512,
    )


def compression_policy_input() -> CompressionPolicyInput:
    """创建合法压缩策略输入。"""
    return CompressionPolicyInput(
        compression_model_profile_id="llmprof_test",
        trigger_tokens=262_144,
        recent_ratio=0.2,
        recent_token_cap=32_768,
        summary_ratio=0.1,
        summary_token_cap=8_192,
    )


@pytest.mark.parametrize(
    ("api_key", "valid"),
    [
        ("a" * 16370, True),
        ("a" * 16371, False),
        ("你" * 5456, True),
        ("你" * 5457, False),
        ("contains\x00nul", False),
        ("  ", False),
    ],
)
def test_model_profile_create_validates_encrypted_api_key_payload(
    api_key: str, valid: bool
) -> None:
    """管理员必须提交非空 API Key，密钥不会进入安全投影。"""
    timestamp = datetime.now(UTC)
    user = actor().user
    repository = _ProfileRepository()
    service = ModelProfileService(
        repository,
        _AuthReader(user),
        clock=lambda: timestamp,
    )
    if valid:
        asyncio.run(service.create(actor(), profile_input(api_key)))
        assert repository.created[-1].api_key == api_key
    else:
        with pytest.raises(ModelProfileError) as error:
            asyncio.run(service.create(actor(), profile_input(api_key)))
        assert error.value.code == "llm_model_credential_invalid"


def test_model_profile_create_revalidates_current_admin() -> None:
    """认证上下文虽携带管理员角色，当前账号降权后仍拒绝写入。"""
    repository = _ProfileRepository()
    service = ModelProfileService(repository, _AuthReader(actor(UserRole.MEMBER).user))
    with pytest.raises(ModelProfileAuthorizationError):
        asyncio.run(service.create(actor(), profile_input()))
    assert repository.created == []


@pytest.mark.parametrize(
    ("context_window_tokens", "valid"),
    [(1_048_576, True), (1_048_577, False)],
)
def test_model_profile_context_window_limit(
    context_window_tokens: int, valid: bool
) -> None:
    """上下文窗口接受 1,048,576 tokens，并拒绝超过上限的值。"""
    repository = _ProfileRepository()
    service = ModelProfileService(repository, _AuthReader(actor().user))
    value = replace(
        profile_input(),
        context_window_tokens=context_window_tokens,
        max_output_tokens=1_000_000,
    )
    if valid:
        asyncio.run(service.create(actor(), value))
        assert repository.created[-1].context_window_tokens == 1_048_576
    else:
        with pytest.raises(ModelProfileError):
            asyncio.run(service.create(actor(), value))


def test_model_profile_output_limit_stays_independent_from_context_limit() -> None:
    """扩大上下文上限不扩大独立的最大输出 token 上限。"""
    repository = _ProfileRepository()
    service = ModelProfileService(repository, _AuthReader(actor().user))
    with pytest.raises(ModelProfileError):
        asyncio.run(
            service.create(
                actor(),
                replace(
                    profile_input(),
                    context_window_tokens=1_048_576,
                    max_output_tokens=1_000_001,
                ),
            )
        )


def test_model_profile_api_dto_context_boundary() -> None:
    """API DTO 接受新的上下文上限，同时拒绝上限加一。"""
    from pydantic import ValidationError

    from api.dto.llm import ModelProfileCreateRequest

    request = ModelProfileCreateRequest(
        name="Large context",
        base_url="https://llm.example/v1",
        model="large-context",
        api_key="test-key",
        context_window_tokens=1_048_576,
        max_output_tokens=1_000_000,
    )
    assert request.context_window_tokens == 1_048_576
    with pytest.raises(ValidationError):
        ModelProfileCreateRequest.model_validate(
            {**request.model_dump(), "context_window_tokens": 1_048_577}
        )


def test_parallel_tool_capability_requires_tools_and_round_trips() -> None:
    repository = _ProfileRepository()
    service = ModelProfileService(repository, _AuthReader(actor().user))

    invalid = replace(
        profile_input(),
        supports_parallel_tool_calls=True,
        supports_tools=False,
    )
    with pytest.raises(ModelProfileError):
        asyncio.run(service.create(actor(), invalid))
    valid = replace(profile_input(), supports_parallel_tool_calls=True)
    created = asyncio.run(service.create(actor(), valid))
    assert created.supports_parallel_tool_calls is True


@pytest.mark.parametrize(
    "field",
    ("trigger_tokens", "recent_token_cap", "summary_token_cap"),
)
def test_compression_policy_dto_enforces_javascript_safe_integer(field: str) -> None:
    """三个 Token 预算统一接受 JS 安全整数上限并拒绝上限加一。"""
    from pydantic import ValidationError

    from api.dto.llm import CompressionPolicyRequest

    body = {
        "compression_model_profile_id": "llmprof_test",
        "trigger_tokens": 262_144,
        "recent_ratio": 0.2,
        "recent_token_cap": 32_768,
        "summary_ratio": 0.1,
        "summary_token_cap": 8_192,
    }
    body[field] = MAX_COMPRESSION_TOKEN_BUDGET
    request = CompressionPolicyRequest.model_validate(body)
    assert getattr(request, field) == MAX_COMPRESSION_TOKEN_BUDGET

    for invalid in (MAX_COMPRESSION_TOKEN_BUDGET + 1, 2**80):
        with pytest.raises(ValidationError):
            CompressionPolicyRequest.model_validate({**body, field: invalid})


@pytest.mark.parametrize(
    "field",
    ("trigger_tokens", "recent_token_cap", "summary_token_cap"),
)
def test_compression_policy_api_rejects_unsafe_token_values_with_422(field: str) -> None:
    """越界 Token 必须在 DTO 边界返回 422，不能进入仓储并形成 500。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.llm import build_llm_admin_router

    class _Authenticator:
        async def authenticate(self, token: str) -> AuthContext | None:
            return actor() if token == "admin" else None

    repository = _CompressionPolicyRepository()
    service = ModelProfileService(repository, _AuthReader(actor().user))
    app = FastAPI()
    app.state.authenticator = _Authenticator()
    app.include_router(build_llm_admin_router(service))
    body = {
        "compression_model_profile_id": "llmprof_test",
        "trigger_tokens": 262_144,
        "recent_ratio": 0.2,
        "recent_token_cap": 32_768,
        "summary_ratio": 0.1,
        "summary_token_cap": 8_192,
    }
    with TestClient(app) as client:
        client.cookies.set("athena_session", "admin")
        for invalid in (MAX_COMPRESSION_TOKEN_BUDGET + 1, 2**80):
            response = client.put(
                "/api/v1/admin/llm/compression-policy",
                json={**body, field: invalid},
            )
            assert response.status_code == 422
    assert repository.policy_values == []


def test_compression_policy_service_accepts_safe_integer_max() -> None:
    """service 直连输入也接受三个字段的安全整数上限。"""
    repository = _CompressionPolicyRepository()
    service = ModelProfileService(repository, _AuthReader(actor().user))
    value = replace(
        compression_policy_input(),
        trigger_tokens=MAX_COMPRESSION_TOKEN_BUDGET,
        recent_token_cap=MAX_COMPRESSION_TOKEN_BUDGET,
        summary_token_cap=MAX_COMPRESSION_TOKEN_BUDGET,
    )
    created = asyncio.run(service.set_compression_policy(actor(), value))
    assert created.trigger_tokens == MAX_COMPRESSION_TOKEN_BUDGET
    assert created.recent_token_cap == MAX_COMPRESSION_TOKEN_BUDGET
    assert created.summary_token_cap == MAX_COMPRESSION_TOKEN_BUDGET
    assert repository.policy_values == [value]


@pytest.mark.parametrize(
    "field",
    ("trigger_tokens", "recent_token_cap", "summary_token_cap"),
)
def test_compression_policy_service_rejects_unsafe_token_values(field: str) -> None:
    """service 直连输入不能绕过 DTO 写入越界 Token 预算。"""
    repository = _CompressionPolicyRepository()
    service = ModelProfileService(repository, _AuthReader(actor().user))
    for invalid in (MAX_COMPRESSION_TOKEN_BUDGET + 1, 2**80):
        if field == "trigger_tokens":
            value = replace(compression_policy_input(), trigger_tokens=invalid)
        elif field == "recent_token_cap":
            value = replace(compression_policy_input(), recent_token_cap=invalid)
        else:
            value = replace(compression_policy_input(), summary_token_cap=invalid)
        with pytest.raises(ModelProfileError) as error:
            asyncio.run(service.set_compression_policy(actor(), value))
        assert error.value.code == "llm_compression_policy_invalid"
    assert repository.policy_values == []


def test_model_profile_admin_api_requires_auth_and_never_returns_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """管理 API 需要管理员登录，配置响应不返回密钥明文。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.llm import build_llm_admin_router

    class _Authenticator:
        async def authenticate(self, token: str) -> AuthContext | None:
            return actor() if token == "valid-admin" else None

    repository = _ProfileRepository()
    service = ModelProfileService(repository, _AuthReader(actor().user))
    app = FastAPI()
    app.state.authenticator = _Authenticator()
    app.include_router(build_llm_admin_router(service))
    body = {
        "name": "Test model",
        "base_url": "https://llm.example/v1",
        "model": "test-model",
        "api_key": "sensitive-value",
        "context_window_tokens": 8192,
        "max_output_tokens": 512,
    }
    with TestClient(app) as client:
        assert client.post("/api/v1/admin/llm/model-profiles", json=body).status_code == 401
        client.cookies.set("athena_session", "valid-admin")
        response = client.post("/api/v1/admin/llm/model-profiles", json=body)
    assert response.status_code == 201
    assert "sensitive-value" not in response.text
    assert response.json()["has_api_key"] is True


def test_compression_policy_admin_api_and_user_denial() -> None:
    """压缩策略 API 拒绝普通用户并只向管理员提供策略版本。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.llm import build_llm_admin_router
    from domain.llm.model.compression_policy import CompressionPolicyVersion

    class _PolicyRepository(_ProfileRepository):
        current: CompressionPolicyVersion | None = None

        async def get_current_compression_policy(self) -> CompressionPolicyVersion | None:
            return self.current

        async def set_compression_policy(
            self, actor_context: AuthContext, value: object
        ) -> CompressionPolicyVersion:
            self.current = CompressionPolicyVersion(
                id="cmpol_api_test",
                compression_model_profile_id="llmprof_test",
                trigger_tokens=262_144,
                recent_ratio=0.2,
                recent_token_cap=32_768,
                summary_ratio=0.1,
                summary_token_cap=8_192,
                prompt_template_version="context-summary-v1",
                created_at=datetime.now(UTC),
            )
            return self.current

    class _Authenticator:
        async def authenticate(self, token: str) -> AuthContext | None:
            if token == "admin":
                return actor(UserRole.SUPER_ADMIN)
            if token == "user":
                return actor(UserRole.MEMBER)
            return None

    repository = _PolicyRepository()
    service = ModelProfileService(repository, _AuthReader(actor().user))
    app = FastAPI()
    app.state.authenticator = _Authenticator()
    app.include_router(build_llm_admin_router(service))
    body = {
        "compression_model_profile_id": "llmprof_test",
        "trigger_tokens": 262_144,
        "recent_ratio": 0.2,
        "recent_token_cap": 32_768,
        "summary_ratio": 0.1,
        "summary_token_cap": 8_192,
    }
    with TestClient(app) as client:
        assert client.get("/api/v1/admin/llm/compression-policy").status_code == 401
        client.cookies.set("athena_session", "user")
        assert client.get("/api/v1/admin/llm/compression-policy").status_code == 403
        assert client.put("/api/v1/admin/llm/compression-policy", json=body).status_code == 403
        client.cookies.set("athena_session", "admin")
        updated = client.put("/api/v1/admin/llm/compression-policy", json=body)
        loaded = client.get("/api/v1/admin/llm/compression-policy")
    assert updated.status_code == loaded.status_code == 200
    assert loaded.json()["id"] == "cmpol_api_test"
    assert loaded.json()["trigger_tokens"] == 262_144
    assert loaded.json()["recent_token_cap"] == 32_768
    assert loaded.json()["summary_ratio"] == 0.1
    assert loaded.json()["summary_token_cap"] == 8_192
