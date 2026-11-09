"""模型版本管理员服务边界测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.llm.profile_service import ModelProfileService
from domain.auth import AuthContext, User, UserRole, UserStatus
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
            credential_env=value.credential_env,
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
        )

    async def list(self, actor: AuthContext) -> tuple[ModelProfileVersion, ...]:
        return ()

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


def actor(role: UserRole = UserRole.ADMIN) -> AuthContext:
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


def profile_input(credential_env: str = "ATHENA_LLM_CREDENTIAL_TEST") -> ModelProfileInput:
    """创建合法配置字段。"""
    return ModelProfileInput(
        name="Test model",
        base_url="https://llm.example/v1",
        model="test-model",
        credential_env=credential_env,
        context_window_tokens=8192,
        max_output_tokens=512,
    )


def test_model_profile_create_requires_credential_namespace_and_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只允许专属环境变量引用，且引用必须已经注入。"""
    timestamp = datetime.now(UTC)
    user = actor().user
    repository = _ProfileRepository()
    service = ModelProfileService(
        repository,
        _AuthReader(user),
        clock=lambda: timestamp,
    )
    monkeypatch.setenv("ATHENA_LLM_CREDENTIAL_TEST", "test-secret")
    asyncio.run(service.create(actor(), profile_input()))
    assert len(repository.created) == 1

    with pytest.raises(ModelProfileError):
        asyncio.run(service.create(actor(), profile_input("DATABASE_URL")))
    with pytest.raises(ModelProfileError):
        asyncio.run(service.create(actor(), profile_input("ATHENA_LLM_CREDENTIAL_MISSING")))
    assert len(repository.created) == 1


def test_model_profile_create_revalidates_current_admin() -> None:
    """认证上下文虽携带管理员角色，当前账号降权后仍拒绝写入。"""
    repository = _ProfileRepository()
    service = ModelProfileService(repository, _AuthReader(actor(UserRole.USER).user))
    with pytest.raises(ModelProfileAuthorizationError):
        asyncio.run(service.create(actor(), profile_input()))
    assert repository.created == []


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

    monkeypatch.setenv("ATHENA_LLM_CREDENTIAL_TEST", "sensitive-value")
    repository = _ProfileRepository()
    service = ModelProfileService(repository, _AuthReader(actor().user))
    app = FastAPI()
    app.state.authenticator = _Authenticator()
    app.include_router(build_llm_admin_router(service))
    body = {
        "name": "Test model",
        "base_url": "https://llm.example/v1",
        "model": "test-model",
        "credential_env": "ATHENA_LLM_CREDENTIAL_TEST",
        "context_window_tokens": 8192,
        "max_output_tokens": 512,
    }
    with TestClient(app) as client:
        assert client.post("/api/v1/admin/llm/model-profiles", json=body).status_code == 401
        client.cookies.set("athena_session", "valid-admin")
        response = client.post("/api/v1/admin/llm/model-profiles", json=body)
    assert response.status_code == 201
    assert "sensitive-value" not in response.text
    assert "credential_env" in response.json()


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
                trigger_ratio=0.8,
                recent_ratio=0.2,
                summary_ratio=0.1,
                prompt_template_version="context-summary-v1",
                created_at=datetime.now(UTC),
            )
            return self.current

    class _Authenticator:
        async def authenticate(self, token: str) -> AuthContext | None:
            if token == "admin":
                return actor(UserRole.ADMIN)
            if token == "user":
                return actor(UserRole.USER)
            return None

    repository = _PolicyRepository()
    service = ModelProfileService(repository, _AuthReader(actor().user))
    app = FastAPI()
    app.state.authenticator = _Authenticator()
    app.include_router(build_llm_admin_router(service))
    body = {
        "compression_model_profile_id": "llmprof_test",
        "trigger_ratio": 0.8,
        "recent_ratio": 0.2,
        "summary_ratio": 0.1,
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
    assert loaded.json()["summary_ratio"] == 0.1
