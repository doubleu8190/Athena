"""绑定到现有 SQLAlchemy 会话的身份复核器与认证写事务行锁守卫。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.auth.converter import user_to_domain
from adapter.auth.model import AuthSessionModel, UserModel
from common.date import as_utc, utc_now
from domain.auth import User, UserStatus


async def verify_active_session_user(
    session: AsyncSession,
    user_id: str,
    session_id: str,
    *,
    clock: Callable[[], datetime] | None = None,
) -> User | None:
    """在当前事务或只读会话中以单条无锁 JOIN 复核有效用户与登录会话。

    严禁对 users / auth_sessions 申请 FOR SHARE 或 FOR UPDATE 行锁，
    避免后台管理写事务与认证核心表产生跨域行锁争用；仍实时拒绝已停用、已删除、
    已撤销、已过期或角色已变更的身份。
    """
    # 延迟求值默认时钟，保持单测对模块级 utc_now 的补丁注入有效。
    current_time = as_utc(utc_now() if clock is None else clock())
    user_row = await session.scalar(
        select(UserModel)
        .join(AuthSessionModel, AuthSessionModel.user_id == UserModel.id)
        .where(
            UserModel.id == user_id,
            UserModel.status == UserStatus.ACTIVE.value,
            UserModel.deleted_time.is_(None),
            AuthSessionModel.id == session_id,
            AuthSessionModel.revoked_time.is_(None),
            AuthSessionModel.expires_at > current_time,
        )
        .execution_options(populate_existing=True)
    )
    return None if user_row is None else user_to_domain(user_row)


class SqlAlchemyAuthTransactionGuard:
    """专供认证仓储修改自身 users 行（改密、改显示名）使用的行锁守卫。

    必须在写操作提交之前保持该 session 的事务打开。统一锁顺序为先 users 行、
    再 auth_sessions 行；其他业务管理仓储应改用无锁的 verify_active_session_user。
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """保存调用方事务会话和锁后读取的 UTC 时钟。

        参数：session 是最终资源写入所使用的 AsyncSession；clock 在两行锁定后读取当前 UTC 时间。
        返回：无。
        异常：不主动抛出业务异常。
        """
        self._session = session
        self._clock = clock

    async def lock_current_session_user_row(
        self,
        user_id: str,
        session_id: str,
        *,
        lock_user_for_update: bool = False,
    ) -> UserModel | None:
        """锁定用户与会话并校验有效身份，返回当前事务内已锁定的 UserModel ORM 行。"""
        # 默认使用 FOR SHARE 共享锁（并发复核互不阻塞，仍与写锁互斥）；写用户自身字段时支持
        # 直接持 FOR UPDATE，避免先共享锁再 UPDATE 触发锁升级竞争。
        user_row = await self._session.scalar(
            select(UserModel)
            .where(
                UserModel.id == user_id,
                UserModel.status == UserStatus.ACTIVE.value,
                UserModel.deleted_time.is_(None),
            )
            .with_for_update(read=not lock_user_for_update)
            .execution_options(populate_existing=True)
        )
        if user_row is None:
            return None

        # 锁 session 前已持有 user 锁；会话撤销持有写锁且只访问 session 行，不反向等待 user。
        auth_row = await self._session.scalar(
            select(AuthSessionModel)
            .where(
                AuthSessionModel.user_id == user_id,
                AuthSessionModel.id == session_id,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if auth_row is None:
            return None
        # 两把行锁都已取得，再读当前时刻并一起检查撤销和过期状态。
        current_time = as_utc(self._clock())
        expires_at = as_utc(auth_row.expires_at)
        if auth_row.revoked_time is not None or expires_at <= current_time:
            return None
        return user_row

    async def lock_current_session_user(
        self,
        user_id: str,
        session_id: str,
    ) -> User | None:
        """锁定用户和会话并读取本事务可见的当前有效身份。

        参数：user_id、session_id 来自已验证的认证上下文。
        返回：账号 active、未删除且会话未撤销、未过期时返回锁定后的当前用户；
        任一条件不满足或标识不匹配时返回 None。
        异常：数据库错误由 SQLAlchemy 原样传播。
        """
        user_row = await self.lock_current_session_user_row(user_id, session_id)
        return None if user_row is None else user_to_domain(user_row)


__all__ = ["SqlAlchemyAuthTransactionGuard", "verify_active_session_user"]
