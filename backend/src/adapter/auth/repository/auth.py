"""用户与登录会话的 async SQLAlchemy 仓储。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.auth.converter.auth import auth_session_to_domain, user_to_domain
from adapter.auth.model.auth import AuthSessionModel, UserModel
from domain.auth import (
    AuthContext,
    AuthRepository,
    AuthSession,
    EmailAlreadyRegisteredError,
    User,
    UserStatus,
)


class SqlAlchemyAuthRepository(AuthRepository):
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
        """原子更新密码并撤销其他设备会话。"""
        async with self._session_factory() as session:
            async with session.begin():
                updated_user_id = await session.scalar(
                    update(UserModel)
                    .where(
                        UserModel.id == user_id,
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


__all__ = ["SqlAlchemyAuthRepository"]
