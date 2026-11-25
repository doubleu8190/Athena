"""用户与登录会话的 async SQLAlchemy 仓储。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.auth.converter.auth import user_to_domain
from adapter.auth.model.auth import AuthSessionModel, UserModel
from adapter.auth.repository.transaction_guard import SqlAlchemyAuthTransactionGuard
from domain.auth import (
    AdminPromotionError,
    AuthContext,
    AuthProvider,
    AuthRepository,
    AuthSession,
    CurrentSessionReader,
    EmailAlreadyRegisteredError,
    User,
    UserDisabledError,
    UserRole,
    UserStatus,
)


def _user_to_model(user: User) -> UserModel:
    """把领域 User 投影为 SQLAlchemy ORM 行。"""
    return UserModel(
        id=user.id,
        email=user.email,
        password_hash=user.password_hash,
        display_name=user.display_name,
        status=user.status.value,
        role=user.role.value,
        auth_provider=user.auth_provider.value,
        external_union_id=user.external_union_id,
        external_open_id=user.external_open_id,
        employee_no=user.employee_no,
        avatar_url=user.avatar_url,
        job_title=user.job_title,
        last_synced_at=user.last_synced_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
        deleted_time=user.deleted_time,
    )


class SqlAlchemyAuthRepository(AuthRepository, CurrentSessionReader):
    """在同一数据库事务中维护用户和登录会话。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """保存异步会话工厂。"""
        self._session_factory = session_factory

    async def create_user(self, user: User) -> User:
        """受控创建单个用户记录；若未删除邮箱已存在则抛 EmailAlreadyRegisteredError。"""
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    session.add(_user_to_model(user))
        except IntegrityError as error:
            if user.email is not None:
                existing = await self.get_user_by_email(user.email)
                if existing is not None:
                    raise EmailAlreadyRegisteredError("email_exists") from error
            raise
        return user

    async def create_user_with_session(
        self,
        user: User,
        auth_session: AuthSession,
    ) -> User:
        """原子创建用户及首个登录会话。"""
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    session.add(_user_to_model(user))
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
            if user.email is not None:
                existing = await self.get_user_by_email(user.email)
                if existing is not None:
                    raise EmailAlreadyRegisteredError("email_exists") from error
            raise
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
        """在同一事务内创建或重置本地 super_admin 应急账号，重置时撤销全部既有会话。"""
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(UserModel)
                    .where(
                        UserModel.email == email,
                        UserModel.deleted_time.is_(None),
                    )
                    .with_for_update()
                )
                if row is not None:
                    # 防止 CLI 意外劫持外部 SSO/Mock 身份记录为本地密码账号。
                    if row.auth_provider != AuthProvider.LOCAL.value:
                        raise AdminPromotionError("email_belongs_to_external_provider")
                    row.password_hash = password_hash
                    row.role = UserRole.SUPER_ADMIN.value
                    row.status = UserStatus.ACTIVE.value
                    if display_name is not None:
                        row.display_name = display_name
                    row.updated_at = now
                    await session.flush()
                    # 应急密码重置后必须撤销全部历史会话，迫使旧凭据立即失效。
                    await session.execute(
                        update(AuthSessionModel)
                        .where(
                            AuthSessionModel.user_id == row.id,
                            AuthSessionModel.revoked_time.is_(None),
                        )
                        .values(revoked_time=now)
                    )
                    return user_to_domain(row), False

                created = UserModel(
                    id=user_id,
                    email=email,
                    password_hash=password_hash,
                    display_name=display_name or "Super Admin",
                    status=UserStatus.ACTIVE.value,
                    role=UserRole.SUPER_ADMIN.value,
                    auth_provider=AuthProvider.LOCAL.value,
                    created_at=now,
                    updated_at=now,
                    deleted_time=None,
                )
                session.add(created)
                await session.flush()
                return user_to_domain(created), True

    async def get_user_by_id(self, user_id: str) -> User | None:
        """按主键读取用户。"""
        async with self._session_factory() as session:
            row = await session.get(UserModel, user_id)
        return None if row is None else user_to_domain(row)

    async def get_user_by_email(self, email: str) -> User | None:
        """按规范邮箱读取未删除用户。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(UserModel).where(
                    UserModel.email == email,
                    UserModel.deleted_time.is_(None),
                )
            )
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
        """按 union_id/open_id 同步飞书用户并签发会话；禁用或已删除用户先吊销全部会话后拒绝。"""
        disabled_detected = False
        result_user: User | None = None

        async with self._session_factory() as session:
            async with session.begin():
                row: UserModel | None = None
                if union_id is not None:
                    row = await session.scalar(
                        select(UserModel)
                        .where(UserModel.external_union_id == union_id)
                        .order_by(UserModel.deleted_time.is_not(None), UserModel.created_at.asc())
                        .with_for_update()
                    )
                if row is None and open_id is not None:
                    row = await session.scalar(
                        select(UserModel)
                        .where(UserModel.external_open_id == open_id)
                        .order_by(UserModel.deleted_time.is_not(None), UserModel.created_at.asc())
                        .with_for_update()
                    )

                if row is not None:
                    # 停用或已软删除用户禁止重新登录，且必须在本次事务内提交撤销其全部既有会话；
                    # 绝不能在 session.begin() 内直接抛异常，否则 SQLAlchemy 会回滚撤销写操作。
                    if (
                        row.status != UserStatus.ACTIVE.value
                        or row.deleted_time is not None
                        or row.auth_provider != AuthProvider.FEISHU.value
                    ):
                        await session.execute(
                            update(AuthSessionModel)
                            .where(
                                AuthSessionModel.user_id == row.id,
                                AuthSessionModel.revoked_time.is_(None),
                            )
                            .values(revoked_time=now)
                        )
                        disabled_detected = True
                    else:
                        if union_id is not None:
                            row.external_union_id = union_id
                        if open_id is not None:
                            row.external_open_id = open_id
                        if display_name is not None:
                            row.display_name = display_name
                        if email is not None:
                            conflict_id = await session.scalar(
                                select(UserModel.id).where(
                                    UserModel.email == email,
                                    UserModel.deleted_time.is_(None),
                                    UserModel.id != row.id,
                                )
                            )
                            if conflict_id is None:
                                row.email = email
                        if avatar_url is not None:
                            row.avatar_url = avatar_url
                        if employee_no is not None:
                            row.employee_no = employee_no
                        if job_title is not None:
                            row.job_title = job_title
                        row.last_synced_at = now
                        row.updated_at = now
                        await session.flush()
                        session.add(
                            AuthSessionModel(
                                id=auth_session_id,
                                user_id=row.id,
                                token_hash=token_hash,
                                expires_at=expires_at,
                                revoked_time=None,
                                created_at=now,
                            )
                        )
                        result_user = user_to_domain(row)
                else:
                    safe_email = email
                    if safe_email is not None:
                        conflict_id = await session.scalar(
                            select(UserModel.id).where(
                                UserModel.email == safe_email,
                                UserModel.deleted_time.is_(None),
                            )
                        )
                        if conflict_id is not None:
                            safe_email = None
                    created = UserModel(
                        id=user_id,
                        email=safe_email,
                        password_hash=None,
                        display_name=display_name,
                        status=UserStatus.ACTIVE.value,
                        role=UserRole.MEMBER.value,
                        auth_provider=AuthProvider.FEISHU.value,
                        external_union_id=union_id,
                        external_open_id=open_id,
                        employee_no=employee_no,
                        avatar_url=avatar_url,
                        job_title=job_title,
                        last_synced_at=now,
                        created_at=now,
                        updated_at=now,
                        deleted_time=None,
                    )
                    session.add(created)
                    await session.flush()
                    session.add(
                        AuthSessionModel(
                            id=auth_session_id,
                            user_id=created.id,
                            token_hash=token_hash,
                            expires_at=expires_at,
                            revoked_time=None,
                            created_at=now,
                        )
                    )
                    result_user = user_to_domain(created)

        # 事务提交后再抛出禁用拒绝异常，确保上面执行的会话批量撤销已落盘。
        if disabled_detected:
            raise UserDisabledError("user_disabled")
        assert result_user is not None
        return result_user

    async def get_active_session(
        self,
        token_hash: str,
        now: datetime,
    ) -> AuthContext | None:
        """按 token 摘要以单条 JOIN 读取有效会话与可用用户，消除每请求的二次串行 SQL。"""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(UserModel, AuthSessionModel.id)
                    .join(AuthSessionModel, AuthSessionModel.user_id == UserModel.id)
                    .where(
                        AuthSessionModel.token_hash == token_hash,
                        AuthSessionModel.revoked_time.is_(None),
                        AuthSessionModel.expires_at > now,
                        UserModel.status == UserStatus.ACTIVE.value,
                        UserModel.deleted_time.is_(None),
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        user_row, session_id = row
        return AuthContext(
            user=user_to_domain(user_row),
            session_id=session_id,
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
        """锁定用户行并幂等提升仍可用的账号为超级管理员。"""
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
                if row.role != UserRole.SUPER_ADMIN.value:
                    row.role = UserRole.SUPER_ADMIN.value
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
        """在写事务内锁定并复核本地用户与当前保留会话后，原子更新密码并撤销其他设备会话。"""
        async with self._session_factory() as session:
            async with session.begin():
                # 复用事务守卫锁定 users 与 auth_sessions 行，复核账号 active/未删除及
                # keep_session_id 仍归属该用户且未撤销、未过期，防止并发登出或封禁后仍改密。
                guard = SqlAlchemyAuthTransactionGuard(session, clock=lambda: updated_at)
                current_user = await guard.lock_current_session_user(user_id, keep_session_id)
                if current_user is None or current_user.auth_provider is not AuthProvider.LOCAL:
                    return False
                updated_user_id = await session.scalar(
                    update(UserModel)
                    .where(
                        UserModel.id == user_id,
                        UserModel.auth_provider == AuthProvider.LOCAL.value,
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
        """复核会话有效性后仅写显示名，避免覆盖并发更新的凭据或角色。"""
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
