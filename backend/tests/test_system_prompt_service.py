"""管理员系统提示词用例与 HTTP 契约测试。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth_middleware import AuthPrecheckMiddleware
from api.system_prompt import build_system_prompt_admin_router
from app.system_prompt import SystemPromptService
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.system_prompt import (
    INITIAL_COMPRESSION_SYSTEM_PROMPT,
    INITIAL_MAIN_SYSTEM_PROMPT,
    INITIAL_SYSTEM_PROMPT_IDS,
    MAX_SYSTEM_PROMPT_CONTENT_CHARACTERS,
    SystemPromptAuthorizationError,
    SystemPromptConflictError,
    SystemPromptError,
    SystemPromptKind,
    SystemPromptNotFoundError,
    SystemPromptUnavailableError,
    SystemPromptVersion,
    SystemPromptVersionPage,
)

NOW = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)


def _user(role: UserRole = UserRole.SUPER_ADMIN) -> User:
    """构造当前认证用户。"""
    return User(
        id="usr_system_prompt",
        email="system-prompt@example.com",
        password_hash="unused",
        display_name=None,
        status=UserStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        role=role,
    )


def _actor(role: UserRole = UserRole.SUPER_ADMIN) -> AuthContext:
    """构造认证上下文；服务仍会通过会话读取器复核当前角色。"""
    return AuthContext(user=_user(role), session_id="auth_system_prompt")


def _version(
    kind: SystemPromptKind,
    *,
    version_id: str | None = None,
    content: str | None = None,
    restored_from_version_id: str | None = None,
    created_at: datetime = NOW,
) -> SystemPromptVersion:
    """构造测试版本。"""
    return SystemPromptVersion(
        id=version_id or INITIAL_SYSTEM_PROMPT_IDS[kind.value],
        kind=kind,
        content=content
        or (
            INITIAL_MAIN_SYSTEM_PROMPT
            if kind is SystemPromptKind.MAIN
            else INITIAL_COMPRESSION_SYSTEM_PROMPT
        ),
        created_by=None,
        created_at=created_at,
        restored_from_version_id=restored_from_version_id,
    )


class _AuthReader:
    """返回当前数据库中可见的用户状态。"""

    def __init__(self, user: User | None) -> None:
        self.user = user
        self.calls = 0

    async def get_current_session_user(
        self,
        user_id: str,
        session_id: str,
        now: datetime,
    ) -> User | None:
        """记录并返回测试用户。"""
        self.calls += 1
        return self.user


class _Repository:
    """为服务边界测试提供可控的版本投影。"""

    def __init__(self) -> None:
        self.current = {
            SystemPromptKind.MAIN: _version(SystemPromptKind.MAIN),
            SystemPromptKind.COMPRESSION: _version(SystemPromptKind.COMPRESSION),
        }
        self.versions = {kind: {value.id: value} for kind, value in self.current.items()}
        self.save_calls: list[tuple[SystemPromptKind, str, str, datetime]] = []
        self.restore_calls: list[tuple[SystemPromptKind, str, str, datetime]] = []
        self.save_error: Exception | None = None
        self.restore_error: Exception | None = None

    async def get_current(self, kind: SystemPromptKind) -> SystemPromptVersion | None:
        """读取可控当前版本。"""
        return self.current.get(kind)

    async def list_versions(
        self,
        kind: SystemPromptKind,
        *,
        page: int,
        page_size: int,
    ) -> SystemPromptVersionPage:
        """返回指定类别全部测试版本。"""
        items = tuple(
            sorted(
                self.versions[kind].values(),
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )
        )
        return SystemPromptVersionPage(
            items=items,
            total=len(items),
            page=page,
            page_size=page_size,
        )

    async def get_version(
        self, kind: SystemPromptKind, version_id: str
    ) -> SystemPromptVersion | None:
        """读取同类别指定版本。"""
        return self.versions[kind].get(version_id)

    async def save(
        self,
        kind: SystemPromptKind,
        *,
        content: str,
        expected_current_version_id: str,
        actor: AuthContext,
        created_at: datetime,
    ) -> SystemPromptVersion:
        """模拟一次成功保存并记录精确正文。"""
        self.save_calls.append((kind, content, expected_current_version_id, created_at))
        if self.save_error is not None:
            raise self.save_error
        value = _version(
            kind,
            version_id="sysp_saved",
            content=content,
            created_at=created_at + timedelta(microseconds=1),
        )
        self.versions[kind][value.id] = value
        self.current[kind] = value
        return value

    async def restore(
        self,
        kind: SystemPromptKind,
        *,
        source_version_id: str,
        expected_current_version_id: str,
        actor: AuthContext,
        created_at: datetime,
    ) -> SystemPromptVersion:
        """模拟恢复操作并记录来源。"""
        self.restore_calls.append(
            (kind, source_version_id, expected_current_version_id, created_at)
        )
        if self.restore_error is not None:
            raise self.restore_error
        source = self.versions[kind].get(source_version_id)
        if source is None:
            raise SystemPromptNotFoundError()
        value = _version(
            kind,
            version_id="sysp_restored",
            content=source.content,
            restored_from_version_id=source.id,
            created_at=created_at + timedelta(microseconds=2),
        )
        self.versions[kind][value.id] = value
        self.current[kind] = value
        return value

    async def get_by_id(self, version_id: str) -> SystemPromptVersion | None:
        """按任意类别标识读取测试版本。"""
        for items in self.versions.values():
            if version_id in items:
                return items[version_id]
        return None

    async def get_pair_by_ids(
        self, main_version_id: str, compression_version_id: str
    ) -> tuple[SystemPromptVersion, SystemPromptVersion] | None:
        """读取测试中的固定两类版本。"""
        main = await self.get_version(SystemPromptKind.MAIN, main_version_id)
        compression = await self.get_version(
            SystemPromptKind.COMPRESSION,
            compression_version_id,
        )
        return None if main is None or compression is None else (main, compression)


def test_service_rechecks_current_admin_before_each_operation() -> None:
    """认证上下文携带管理员角色但当前账号降权时，所有操作都被拒绝。"""
    repository = _Repository()
    reader = _AuthReader(_user(UserRole.MEMBER))
    service = SystemPromptService(repository, reader, clock=lambda: NOW)  # type: ignore[arg-type]

    operations = (
        service.get_current(_actor(), SystemPromptKind.MAIN),
        service.list_versions(_actor(), SystemPromptKind.MAIN, page=1, page_size=20),
        service.get_version(
            _actor(),
            SystemPromptKind.MAIN,
            INITIAL_SYSTEM_PROMPT_IDS["main"],
        ),
        service.save(
            _actor(),
            SystemPromptKind.MAIN,
            content="new prompt",
            expected_current_version_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
        ),
        service.restore(
            _actor(),
            SystemPromptKind.MAIN,
            source_version_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
            expected_current_version_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
        ),
    )
    for operation in operations:
        with pytest.raises(SystemPromptAuthorizationError):
            asyncio.run(operation)
    assert reader.calls == 5
    assert repository.save_calls == []
    assert repository.restore_calls == []


@pytest.mark.parametrize(
    "content",
    ("", " \n\t ", "x" * (MAX_SYSTEM_PROMPT_CONTENT_CHARACTERS + 1)),
)
def test_service_rejects_blank_and_oversized_content(content: str) -> None:
    """全空白与超限正文在进入仓储前被拒绝。"""
    repository = _Repository()
    service = SystemPromptService(
        repository,  # type: ignore[arg-type]
        _AuthReader(_user()),
        clock=lambda: NOW,
    )

    with pytest.raises(SystemPromptError):
        asyncio.run(
            service.save(
                _actor(),
                SystemPromptKind.MAIN,
                content=content,
                expected_current_version_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
            )
        )
    assert repository.save_calls == []


def test_service_preserves_exact_content_and_uses_business_clock() -> None:
    """合法正文的换行与首尾空白原样传递，CAS 基线和业务时钟保持精确。"""
    repository = _Repository()
    service = SystemPromptService(
        repository,  # type: ignore[arg-type]
        _AuthReader(_user()),
        clock=lambda: NOW,
    )
    content = "  第一行\n\n第二行  "

    result = asyncio.run(
        service.save(
            _actor(),
            SystemPromptKind.MAIN,
            content=content,
            expected_current_version_id=INITIAL_SYSTEM_PROMPT_IDS["main"],
        )
    )

    assert result.content == content
    assert repository.save_calls == [
        (
            SystemPromptKind.MAIN,
            content,
            INITIAL_SYSTEM_PROMPT_IDS["main"],
            NOW,
        )
    ]


def test_service_distinguishes_missing_current_and_missing_version() -> None:
    """当前配置缺失映射为 unavailable，历史版本缺失映射为 not found。"""
    repository = _Repository()
    repository.current[SystemPromptKind.MAIN] = None  # type: ignore[assignment]
    service = SystemPromptService(repository, _AuthReader(_user()), clock=lambda: NOW)  # type: ignore[arg-type]

    with pytest.raises(SystemPromptUnavailableError):
        asyncio.run(service.get_current(_actor(), SystemPromptKind.MAIN))
    with pytest.raises(SystemPromptNotFoundError):
        asyncio.run(
            service.get_version(
                _actor(),
                SystemPromptKind.MAIN,
                "sysp_missing",
            )
        )


def test_admin_api_contract_permission_validation_and_error_mapping() -> None:
    """管理员 API 校验来源、权限和输入，并映射 404/409/503 稳定错误码。"""

    class _Authenticator:
        async def authenticate(self, token: str) -> AuthContext | None:
            if token == "admin":
                return _actor()
            if token == "user":
                return _actor(UserRole.MEMBER)
            return None

    repository = _Repository()
    admin_user = _user()
    reader = _AuthReader(admin_user)
    service = SystemPromptService(repository, reader, clock=lambda: NOW)  # type: ignore[arg-type]
    app = FastAPI()
    app.state.authenticator = _Authenticator()
    app.state.auth_cookie_name = "athena_session"
    app.state.public_origin = None
    app.add_middleware(AuthPrecheckMiddleware, fastapi_app=app)
    app.include_router(build_system_prompt_admin_router(service))

    with TestClient(app) as client:
        current_path = "/api/v1/admin/system-prompts/main"
        assert client.get(current_path).status_code == 401
        client.cookies.set("athena_session", "user")
        assert client.get(current_path).status_code == 403

        client.cookies.set("athena_session", "admin")
        current = client.get(current_path)
        assert current.status_code == 200
        assert current.json()["content"] == INITIAL_MAIN_SYSTEM_PROMPT

        saved = client.post(
            f"{current_path}/versions",
            json={
                "content": "新的主提示词\n",
                "expected_current_version_id": INITIAL_SYSTEM_PROMPT_IDS["main"],
            },
        )
        assert saved.status_code == 200
        assert saved.json()["id"] == "sysp_saved"
        assert saved.json()["content"] == "新的主提示词\n"

        versions = client.get(f"{current_path}/versions?page=1&page_size=1")
        detail = client.get(f"{current_path}/versions/sysp_saved")
        assert versions.status_code == 200
        assert versions.json()["items"][0]["id"] == "sysp_saved"
        assert "content" not in versions.json()["items"][0]
        assert detail.status_code == 200
        assert detail.json()["content"] == "新的主提示词\n"

        restored = client.post(
            f"{current_path}/restore",
            json={
                "source_version_id": "sysp_saved",
                "expected_current_version_id": "sysp_saved",
            },
        )
        assert restored.status_code == 200
        assert restored.json()["id"] == "sysp_restored"
        assert restored.json()["content"] == "新的主提示词\n"
        assert restored.json()["restored_from_version_id"] == "sysp_saved"

        assert (
            client.post(
                f"{current_path}/versions",
                json={
                    "content": " \n",
                    "expected_current_version_id": INITIAL_SYSTEM_PROMPT_IDS["main"],
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"{current_path}/versions",
                content='{"content":"x","expected_current_version_id":"x","extra":true}',
                headers={"content-type": "application/json"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"{current_path}/restore",
                json={
                    "source_version_id": "sysp_missing",
                    "expected_current_version_id": "sysp_restored",
                },
            ).status_code
            == 404
        )

        repository.save_error = SystemPromptConflictError()
        conflict = client.post(
            f"{current_path}/versions",
            json={
                "content": "conflicting edit",
                "expected_current_version_id": "sysp_saved",
            },
        )
        assert conflict.status_code == 409
        assert conflict.json() == {"detail": "system_prompt_version_conflict"}

        denied_origin = client.post(
            f"{current_path}/versions",
            json={
                "content": "cross origin",
                "expected_current_version_id": "sysp_saved",
            },
            headers={"Origin": "https://attacker.example"},
        )
        assert denied_origin.status_code == 403
        assert denied_origin.json() == {"detail": "origin_not_allowed"}

        repository.current[SystemPromptKind.COMPRESSION] = None  # type: ignore[assignment]
        unavailable = client.get("/api/v1/admin/system-prompts/compression")
        assert unavailable.status_code == 503
        assert unavailable.json() == {"detail": "system_prompt_unavailable"}

        reader.user = replace(admin_user, role=UserRole.MEMBER)
        demoted = client.get(current_path)
        assert demoted.status_code == 403
        assert demoted.json() == {"detail": "admin_required"}
