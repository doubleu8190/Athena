"""用户与登录会话的 async SQLAlchemy 仓储。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.auth.converter.auth import auth_session_to_domain, user_to_domain
from adapter.auth.model.auth import AuthSessionModel, UserModel
from adapter.auth.repository.transaction_guard import SqlAlchemyAuthTransactionGuard
from domain.auth import (
    AuthContext,
    AuthRepository,
    AuthSession,
    CurrentSessionReader,
    EmailAlreadyRegisteredError,
    User,
    UserRole,
    UserStatus,
)


class SqlAlchemyAuthRepository(AuthRepository, CurrentSessionReader):
    """在同一数据库事务中维护用户和登录会话。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """保存异步会话工厂。"""
        self._session_factory = session_factory

    async def create_user_with_session(
        self,
        user: User,
        auth_session: AuthSession,
    ) -> User:
        """原子创建用户及首个登录会话。"""
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    session.add(
                        UserModel(
                            id=user.id,
                            email=user.email,
                            password_hash=user.password_hash,
                            display_name=user.display_name,
                            status=user.status.value,
                            role=user.role.value,
                            created_at=user.created_at,
                            updated_at=user.updated_at,
                            deleted_time=user.deleted_time,
                        )
                    )
                    await session.flush()
                    session.add(
                        AuthSessionModel(
                            id=auth_session.id,
                            user_id=auth_session.user_id,
                            token_hash=auth_session.token_hash,
                            expires_at=auth_session.expires_at,
                            revoked_time=auth_session.revoked_time,
                            created_at=auth_session.created_at,
                        )
                    )
        except IntegrityError as error:
            existing = await self.get_user_by_email(user.email)
            if existing is not None:
                raise EmailAlreadyRegisteredError("email_exists") from error
            raise
        return user

    async def get_user_by_email(self, email: str) -> User | None:
        """按规范邮箱读取用户。"""
        async with self._session_factory() as session:
            row = await session.scalar(select(UserModel).where(UserModel.email == email))
        return None if row is None else user_to_domain(row)

    async def create_session(self, auth_session: AuthSession) -> AuthSession:
        """插入新的登录会话。"""
        async with self._session_factory() as session:
            async with session.begin():
                session.add(
                    AuthSessionModel(
                        id=auth_session.id,
                        user_id=auth_session.user_id,
                        token_hash=auth_session.token_hash,
                        expires_at=auth_session.expires_at,
                        revoked_time=auth_session.revoked_time,
                        created_at=auth_session.created_at,
                    )
                )
        return auth_session

    async def get_active_session(
        self,
        token_hash: str,
        now: datetime,
    ) -> AuthContext | None:
        """按 token 摘要读取有效会话与可用用户。"""
        async with self._session_factory() as session:
            auth_row = await session.scalar(
                select(AuthSessionModel).where(
                    AuthSessionModel.token_hash == token_hash,
                    AuthSessionModel.revoked_time.is_(None),
                    AuthSessionModel.expires_at > now,
                )
            )
            if auth_row is None:
                return None
            user_row = await session.scalar(
                select(UserModel).where(
                    UserModel.id == auth_row.user_id,
                    UserModel.status == UserStatus.ACTIVE.value,
                    UserModel.deleted_time.is_(None),
                )
            )
        if user_row is None:
            return None
        return AuthContext(
            user=user_to_domain(user_row),
            session_id=auth_session_to_domain(auth_row).id,
        )

    async def get_current_session_user(
        self,
        user_id: str,
        session_id: str,
        now: datetime,
    ) -> User | None:
        """从数据库重新验证会话并返回用户当前角色。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(UserModel)
                .join(AuthSessionModel, AuthSessionModel.user_id == UserModel.id)
                .where(
                    UserModel.id == user_id,
                    UserModel.status == UserStatus.ACTIVE.value,
                    UserModel.deleted_time.is_(None),
                    AuthSessionModel.id == session_id,
                    AuthSessionModel.revoked_time.is_(None),
                    AuthSessionModel.expires_at > now,
                )
            )
        return None if row is None else user_to_domain(row)

    async def promote_active_user_by_email(
        self,
        email: str,
        updated_at: datetime,
    ) -> User | None:
        """锁定用户行并幂等提升仍可用的账号。"""
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(UserModel)
                    .where(
                        UserModel.email == email,
                        UserModel.status == UserStatus.ACTIVE.value,
                        UserModel.deleted_time.is_(None),
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                if row.role != UserRole.ADMIN.value:
                    row.role = UserRole.ADMIN.value
                    row.updated_at = updated_at
                    await session.flush()
                return user_to_domain(row)

    async def revoke_session(
        self,
        token_hash: str,
        revoked_at: datetime,
    ) -> bool:
        """撤销未撤销的登录会话。"""
        async with self._session_factory() as session:
            async with session.begin():
                revoked_id = await session.scalar(
                    update(AuthSessionModel)
                    .where(
                        AuthSessionModel.token_hash == token_hash,
                        AuthSessionModel.revoked_time.is_(None),
                    )
                    .values(revoked_time=revoked_at)
                    .returning(AuthSessionModel.id)
                )
        return revoked_id is not None

    async def change_password_and_revoke_sessions(
        self,
        user_id: str,
        password_hash: str,
        updated_at: datetime,
        *,
        keep_session_id: str,
        revoked_at: datetime,
    ) -> bool:
        """在写事务内锁定并复核用户与当前保留会话后，原子更新密码并撤销其他设备会话。"""
        async with self._session_factory() as session:
            async with session.begin():
                # 复用事务守卫锁定 users 与 auth_sessions 行，复核账号 active/未删除及
                # keep_session_id 仍归属该用户且未撤销、未过期，防止并发登出或封禁后仍改密。
                guard = SqlAlchemyAuthTransactionGuard(session, clock=lambda: updated_at)
                current_user = await guard.lock_current_session_user(user_id, keep_session_id)
                if current_user is None:
                    return False
                updated_user_id = await session.scalar(
                    update(UserModel)
                    .where(
                        UserModel.id == user_id,
                        UserModel.status == UserStatus.ACTIVE.value,
                        UserModel.deleted_time.is_(None),
                    )
                    .values(
                        password_hash=password_hash,
                        updated_at=updated_at,
                    )
                    .returning(UserModel.id)
                )
                if updated_user_id is None:
                    return False
                await session.execute(
                    update(AuthSessionModel)
                    .where(
                        AuthSessionModel.user_id == user_id,
                        AuthSessionModel.id != keep_session_id,
                        AuthSessionModel.revoked_time.is_(None),
                    )
                    .values(revoked_time=revoked_at)
                )
        return True

    async def update_display_name(
        self,
        user_id: str,
        session_id: str,
        display_name: str | None,
        updated_at: datetime,
    ) -> User | None:
        """复核会话有效性后仅写显示名，避免覆盖并发更新的凭据或角色。

        参数：user_id 和 session_id 来自当前 Cookie 身份；display_name 是已规范化名称；
        updated_at 是服务层提供的 UTC 更新时间。
        返回：身份仍有效时返回更新后的用户，否则返回 None。
        异常：数据库错误由 SQLAlchemy 原样传播。
        """
        async with self._session_factory() as session:
            async with session.begin():
                guard = SqlAlchemyAuthTransactionGuard(session)
                current_user = await guard.lock_current_session_user(user_id, session_id)
                if current_user is None:
                    return None
                row = await session.get(UserModel, user_id, populate_existing=True)
                if row is None:
                    return None
                row.display_name = display_name
                row.updated_at = updated_at
                await session.flush()
                return user_to_domain(row)


__all__ = ["SqlAlchemyAuthRepository"]
