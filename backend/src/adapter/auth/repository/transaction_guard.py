"""绑定到现有 SQLAlchemy 事务的管理员身份复核器。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.auth.converter.auth import user_to_domain
from adapter.auth.model.auth import AuthSessionModel, UserModel
from common.ids import now_utc
from domain.auth import AuthTransactionGuard, User, UserStatus


class SqlAlchemyAuthTransactionGuard(AuthTransactionGuard):
    """复用调用方 AsyncSession，在最终写事务中锁定并复核授权身份。

    必须在写操作提交之前保持该 session 的事务打开。统一锁顺序为先 users 行、
    再 auth_sessions 行；角色修改也锁 users，撤销只锁 auth_sessions，不形成反向锁链。
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """保存调用方事务会话和锁后读取的 UTC 时钟。

        参数：session 是最终资源写入所使用的 AsyncSession；clock 在两行锁定后读取当前 UTC 时间。
        返回：无。
        异常：不主动抛出业务异常。
        """
        self._session = session
        self._clock = clock

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
        # 管理员角色更新与最终授权复核都先锁用户，因此角色结果在线性化点确定。
        user_row = await self._session.scalar(
            select(UserModel)
            .where(
                UserModel.id == user_id,
                UserModel.status == UserStatus.ACTIVE.value,
                UserModel.deleted_time.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if user_row is None:
            return None

        # 锁 session 前已持有 user 锁；撤销只访问 session 行，不会反向等待 user。
        auth_row = await self._session.scalar(
            select(AuthSessionModel)
            .where(
                AuthSessionModel.user_id == user_id,
                AuthSessionModel.id == session_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if auth_row is None:
            return None
        # 两把行锁都已取得，再读当前时刻并一起检查撤销和过期状态。
        current_time = self._clock()
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)
        else:
            current_time = current_time.astimezone(UTC)
        expires_at = auth_row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        else:
            expires_at = expires_at.astimezone(UTC)
        if auth_row.revoked_time is not None or expires_at <= current_time:
            return None
        return user_to_domain(user_row)


__all__ = ["SqlAlchemyAuthTransactionGuard"]
