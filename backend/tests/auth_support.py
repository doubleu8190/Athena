"""HTTP 测试使用的真实认证服务与内存持久化实现。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from types import TracebackType
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.service import AuthService
from domain.auth import (
    AdminPromotionError,
    AuthContext,
    AuthProvider,
    AuthRepository,
    AuthSession,
    EmailAlreadyRegisteredError,
    PasswordHasher,
    User,
    UserDisabledError,
    UserRole,
    UserStatus,
)

TEST_PASSWORD = "long-enough-password"


class FakePasswordHasher(PasswordHasher):
    """快速、可观测的确定性密码哈希器。"""

    dummy_hash = "fake:dummy"

    def __init__(self) -> None:
        self.verify_calls: list[tuple[str, str]] = []

    def hash_password(self, password: str) -> str:
        return f"fake:{password}"

    def verify_password(self, password: str, password_hash: str) -> bool:
        self.verify_calls.append((password, password_hash))
        return password_hash != self.dummy_hash and password_hash == f"fake:{password}"


class InMemoryAuthRepository(AuthRepository):
    """测试使用的内存认证仓储。"""

    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.sessions: dict[str, AuthSession] = {}

    async def create_user(self, user: User) -> User:
        if user.email is not None and any(
            existing.email == user.email and existing.deleted_time is None
            for existing in self.users.values()
        ):
            raise EmailAlreadyRegisteredError("email_exists")
        self.users[user.id] = user
        return user

    async def upsert_local_super_admin(
        self,
        *,
        user_id: str,
        email: str,
        password_hash: str,
        display_name: str | None,
        now: datetime,
    ) -> tuple[User, bool]:
        existing = await self.get_user_by_email(email)
        if existing is not None:
            if existing.auth_provider is not AuthProvider.LOCAL:
                raise AdminPromotionError("email_belongs_to_external_provider")
            updated = replace(
                existing,
                password_hash=password_hash,
                role=UserRole.SUPER_ADMIN,
                status=UserStatus.ACTIVE,
                display_name=display_name if display_name is not None else existing.display_name,
                updated_at=now,
            )
            self.users[existing.id] = updated
            for token_hash, auth_session in tuple(self.sessions.items()):
                if auth_session.user_id == existing.id and auth_session.revoked_time is None:
                    self.sessions[token_hash] = replace(auth_session, revoked_time=now)
            return updated, False
        created = User(
            id=user_id,
            email=email,
            password_hash=password_hash,
            display_name=display_name or "Super Admin",
            status=UserStatus.ACTIVE,
            role=UserRole.SUPER_ADMIN,
            auth_provider=AuthProvider.LOCAL,
            created_at=now,
            updated_at=now,
        )
        self.users[created.id] = created
        return created, True

    async def get_user_by_id(self, user_id: str) -> User | None:
        return self.users.get(user_id)

    async def get_user_by_email(self, email: str) -> User | None:
        return next(
            (
                user
                for user in self.users.values()
                if user.email == email and user.deleted_time is None
            ),
            None,
        )

    async def create_session(self, auth_session: AuthSession) -> AuthSession:
        self.sessions[auth_session.token_hash] = auth_session
        return auth_session

    async def upsert_feishu_user_and_create_session(
        self,
        *,
        user_id: str,
        union_id: str | None,
        open_id: str | None,
        display_name: str | None,
        email: str | None,
        avatar_url: str | None,
        employee_no: str | None,
        job_title: str | None,
        auth_session_id: str,
        token_hash: str,
        expires_at: datetime,
        now: datetime,
    ) -> User:
        matched: User | None = None
        if union_id is not None:
            matched = next(
                (u for u in self.users.values() if u.external_union_id == union_id),
                None,
            )
        if matched is None and open_id is not None:
            matched = next(
                (u for u in self.users.values() if u.external_open_id == open_id),
                None,
            )
        if matched is not None:
            if (
                matched.status is not UserStatus.ACTIVE
                or matched.deleted_time is not None
                or matched.auth_provider is not AuthProvider.FEISHU
            ):
                for s_hash, auth_session in tuple(self.sessions.items()):
                    if auth_session.user_id == matched.id and auth_session.revoked_time is None:
                        self.sessions[s_hash] = replace(auth_session, revoked_time=now)
                raise UserDisabledError("user_disabled")
            safe_email = matched.email
            if email is not None and not any(
                other.id != matched.id and other.deleted_time is None and other.email == email
                for other in self.users.values()
            ):
                safe_email = email
            updated = replace(
                matched,
                external_union_id=union_id or matched.external_union_id,
                external_open_id=open_id or matched.external_open_id,
                display_name=display_name if display_name is not None else matched.display_name,
                email=safe_email,
                avatar_url=avatar_url if avatar_url is not None else matched.avatar_url,
                employee_no=employee_no if employee_no is not None else matched.employee_no,
                job_title=job_title if job_title is not None else matched.job_title,
                last_synced_at=now,
                updated_at=now,
            )
            self.users[updated.id] = updated
            self.sessions[token_hash] = AuthSession(
                id=auth_session_id,
                user_id=updated.id,
                token_hash=token_hash,
                expires_at=expires_at,
                revoked_time=None,
                created_at=now,
            )
            return updated

        safe_email = email
        if safe_email is not None and any(
            other.deleted_time is None and other.email == safe_email
            for other in self.users.values()
        ):
            safe_email = None
        created = User(
            id=user_id,
            email=safe_email,
            password_hash=None,
            display_name=display_name,
            status=UserStatus.ACTIVE,
            role=UserRole.MEMBER,
            auth_provider=AuthProvider.FEISHU,
            external_union_id=union_id,
            external_open_id=open_id,
            employee_no=employee_no,
            avatar_url=avatar_url,
            job_title=job_title,
            last_synced_at=now,
            created_at=now,
            updated_at=now,
        )
        self.users[created.id] = created
        self.sessions[token_hash] = AuthSession(
            id=auth_session_id,
            user_id=created.id,
            token_hash=token_hash,
            expires_at=expires_at,
            revoked_time=None,
            created_at=now,
        )
        return created

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

    async def get_current_session_user(
        self,
        user_id: str,
        session_id: str,
        now: datetime,
    ) -> User | None:
        session = next((item for item in self.sessions.values() if item.id == session_id), None)
        user = self.users.get(user_id)
        if (
            session is None
            or session.user_id != user_id
            or session.revoked_time is not None
            or session.expires_at <= now
            or user is None
            or user.status is not UserStatus.ACTIVE
            or user.deleted_time is not None
        ):
            return None
        return user

    async def promote_active_user_by_email(
        self,
        email: str,
        updated_at: datetime,
    ) -> User | None:
        user = await self.get_user_by_email(email)
        if user is None or user.status is not UserStatus.ACTIVE or user.deleted_time is not None:
            return None
        promoted = replace(user, role=UserRole.SUPER_ADMIN, updated_at=updated_at)
        self.users[user.id] = promoted
        return promoted

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
        if (
            user is None
            or user.deleted_time is not None
            or user.auth_provider is not AuthProvider.LOCAL
        ):
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

    async def update_display_name(
        self,
        user_id: str,
        session_id: str,
        display_name: str | None,
        updated_at: datetime,
    ) -> User | None:
        """仅修改内存用户显示名并验证会话仍有效。"""
        user = self.users.get(user_id)
        active = any(
            session.id == session_id
            and session.user_id == user_id
            and session.revoked_time is None
            and session.expires_at > updated_at
            for session in self.sessions.values()
        )
        if user is None or not active:
            return None
        updated = replace(user, display_name=display_name, updated_at=updated_at)
        self.users[user_id] = updated
        return updated


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


def provision_local_user_sync(
    service: AuthService,
    *,
    email: str = "user@example.com",
    password: str = TEST_PASSWORD,
    display_name: str | None = None,
    role: UserRole = UserRole.MEMBER,
) -> None:
    """内部受控置备本地测试账号（若邮箱已存在则幂等跳过），替代已下线的公开注册路由。"""

    async def _provision() -> None:
        try:
            await service.provision_local_user(
                email,
                password,
                display_name=display_name,
                role=role,
            )
        except EmailAlreadyRegisteredError:
            pass

    asyncio.run(_provision())


class AuthTestClient:
    """进入上下文时内部置备本地测试账号并通过 /api/v1/auth/login 建立真实认证 Cookie。"""

    def __init__(
        self,
        app: FastAPI,
        *,
        email: str = "user@example.com",
        password: str = TEST_PASSWORD,
        display_name: str | None = None,
        role: UserRole = UserRole.MEMBER,
    ) -> None:
        self._app = app
        self._email = email
        self._password = password
        self._display_name = display_name
        self._role = role
        self._client: TestClient | None = None
        self.user_id: str | None = None

    def __enter__(self) -> TestClient:
        auth_service = cast(AuthService | None, getattr(self._app.state, "authenticator", None))
        if auth_service is None:
            raise AssertionError("app.state.authenticator is required for AuthTestClient")
        # 公开注册路由已彻底下线：改为内部受控置备本地账号，再通过真实登录路由签发 Cookie。
        provision_local_user_sync(
            auth_service,
            email=self._email,
            password=self._password,
            display_name=self._display_name,
            role=self._role,
        )
        client = TestClient(self._app)
        entered = client.__enter__()
        response = entered.post(
            "/api/v1/auth/login",
            json={
                "email": self._email,
                "password": self._password,
            },
        )
        if response.status_code != 200:
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
    "provision_local_user_sync",
]
