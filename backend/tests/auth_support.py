"""HTTP 测试使用的真实认证服务与内存持久化实现。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from types import TracebackType

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.service import AuthService
from domain.auth import (
    AuthContext,
    AuthRepository,
    AuthSession,
    EmailAlreadyRegisteredError,
    PasswordHasher,
    User,
    UserStatus,
)

TEST_PASSWORD = "long-enough-password"


class FakePasswordHasher(PasswordHasher):
    """快速确定性密码哈希器。"""

    dummy_hash = "fake:dummy"

    def hash_password(self, password: str) -> str:
        return f"fake:{password}"

    def verify_password(self, password: str, password_hash: str) -> bool:
        return password_hash != self.dummy_hash and password_hash == f"fake:{password}"


class InMemoryAuthRepository(AuthRepository):
    """测试使用的内存认证仓储。"""

    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.sessions: dict[str, AuthSession] = {}

    async def create_user_with_session(
        self,
        user: User,
        auth_session: AuthSession,
    ) -> User:
        if any(existing.email == user.email for existing in self.users.values()):
            raise EmailAlreadyRegisteredError("email_exists")
        self.users[user.id] = user
        self.sessions[auth_session.token_hash] = auth_session
        return user

    async def get_user_by_email(self, email: str) -> User | None:
        return next(
            (user for user in self.users.values() if user.email == email),
            None,
        )

    async def create_session(self, auth_session: AuthSession) -> AuthSession:
        self.sessions[auth_session.token_hash] = auth_session
        return auth_session

    async def get_active_session(
        self,
        token_hash: str,
        now: datetime,
    ) -> AuthContext | None:
        auth_session = self.sessions.get(token_hash)
        if (
            auth_session is None
            or auth_session.revoked_time is not None
            or auth_session.expires_at <= now
        ):
            return None
        user = self.users.get(auth_session.user_id)
        if user is None or user.status is not UserStatus.ACTIVE or user.deleted_time is not None:
            return None
        return AuthContext(user=user, session_id=auth_session.id)

    async def revoke_session(
        self,
        token_hash: str,
        revoked_at: datetime,
    ) -> bool:
        auth_session = self.sessions.get(token_hash)
        if auth_session is None or auth_session.revoked_time is not None:
            return False
        self.sessions[token_hash] = replace(auth_session, revoked_time=revoked_at)
        return True

    async def change_password_and_revoke_sessions(
        self,
        user_id: str,
        password_hash: str,
        updated_at: datetime,
        *,
        keep_session_id: str,
        revoked_at: datetime,
    ) -> bool:
        user = self.users.get(user_id)
        if user is None or user.deleted_time is not None:
            return False
        self.users[user_id] = replace(
            user,
            password_hash=password_hash,
            updated_at=updated_at,
        )
        for token_hash, auth_session in tuple(self.sessions.items()):
            if (
                auth_session.user_id == user_id
                and auth_session.id != keep_session_id
                and auth_session.revoked_time is None
            ):
                self.sessions[token_hash] = replace(
                    auth_session,
                    revoked_time=revoked_at,
                )
        return True


def incrementing_factory(prefix: str) -> Callable[[], str]:
    """创建带固定前缀的递增 ID/token 工厂。"""
    counter = 0

    def create() -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}{counter}"

    return create


def make_auth_service() -> AuthService:
    """创建使用内存仓储和确定性 ID 的认证服务。"""
    return AuthService(
        InMemoryAuthRepository(),
        FakePasswordHasher(),
        user_id_factory=incrementing_factory("user_"),
        auth_session_id_factory=incrementing_factory("session_"),
        token_factory=incrementing_factory("token_"),
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )


class AuthTestClient:
    """进入上下文时注册或登录真实认证 Cookie 的测试客户端。"""

    def __init__(
        self,
        app: FastAPI,
        *,
        email: str = "user@example.com",
        password: str = TEST_PASSWORD,
        display_name: str | None = None,
    ) -> None:
        self._app = app
        self._email = email
        self._password = password
        self._display_name = display_name
        self._client: TestClient | None = None
        self.user_id: str | None = None

    def __enter__(self) -> TestClient:
        client = TestClient(self._app)
        entered = client.__enter__()
        response = entered.post(
            "/api/v1/auth/register",
            json={
                "email": self._email,
                "password": self._password,
                "display_name": self._display_name,
            },
        )
        if response.status_code == 409:
            response = entered.post(
                "/api/v1/auth/login",
                json={
                    "email": self._email,
                    "password": self._password,
                },
            )
        if response.status_code != 200 and response.status_code != 201:
            client.__exit__(None, None, None)
            raise AssertionError(f"authentication failed: {response.status_code} {response.text}")
        self._client = client
        self.user_id = response.json()["id"]
        return entered

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._client is not None:
            self._client.__exit__(exc_type, exc, traceback)


__all__ = [
    "AuthTestClient",
    "FakePasswordHasher",
    "InMemoryAuthRepository",
    "TEST_PASSWORD",
    "incrementing_factory",
    "make_auth_service",
]
