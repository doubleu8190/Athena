"""PostgreSQL 租约中心和可复用的事务内 fencing 操作。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.leases import LeaseGrant, LeaseKey, LeaseLostError
from ..models import ExecutionLeaseModel


class PostgresLeaseCenter:
    """为不同队列提供同一套租约状态转换。"""

    def __init__(
        self,
        session_factory: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ],
    ) -> None:
        """保存用于心跳续租的 PostgreSQL 会话工厂。"""
        self._session_factory = session_factory

    async def acquire(
        self,
        key: LeaseKey,
        *,
        owner_id: str,
        lease_seconds: float,
    ) -> LeaseGrant | None:
        """在独立短事务中申请租约，供非队列业务入口使用。"""
        async with self._session_factory() as session:
            async with session.begin():
                return await self.acquire_in_transaction(
                    session,
                    key,
                    owner_id=owner_id,
                    lease_seconds=lease_seconds,
                )

    async def acquire_in_transaction(
        self,
        session: AsyncSession,
        key: LeaseKey,
        *,
        owner_id: str,
        lease_seconds: float,
    ) -> LeaseGrant | None:
        """在调用方事务中创建或接管指定资源的执行权。

        Args:
            session: 调用方持有的活动数据库事务。
            key: 要锁定的资源类型和资源 ID。
            owner_id: 本次领取唯一身份，不得复用 Worker 静态 ID。
            lease_seconds: 新租约有效秒数，必须为正数。

        Returns:
            成功时返回新 token 的 grant；仍有有效持有者时返回 None。

        Raises:
            ValueError: owner 为空或租期无效。
        """
        if not owner_id.strip() or lease_seconds <= 0:
            raise ValueError("lease owner and positive duration are required")

        await session.execute(
            pg_insert(ExecutionLeaseModel)
            .values(
                resource_type=key.resource_type,
                resource_id=key.resource_id,
                owner_id=None,
                fencing_token=0,
                state="released",
                lease_until=None,
                created_at=func.clock_timestamp(),
                updated_at=func.clock_timestamp(),
            )
            .on_conflict_do_nothing(
                index_elements=["resource_type", "resource_id"]
            )
        )
        row = await session.scalar(
            select(ExecutionLeaseModel)
            .where(
                ExecutionLeaseModel.resource_type == key.resource_type,
                ExecutionLeaseModel.resource_id == key.resource_id,
            )
            .with_for_update()
        )
        if row is None:
            raise RuntimeError("lease_record_not_created")
        now = await session.scalar(select(func.clock_timestamp()))
        if now is None:
            raise RuntimeError("database_clock_unavailable")
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        if (
            row.state in {"active", "revoked"}
            and row.lease_until is not None
            and row.lease_until > now
        ):
            return None

        row.owner_id = owner_id
        row.fencing_token += 1
        row.state = "active"
        row.lease_until = now + timedelta(seconds=lease_seconds)
        row.updated_at = now
        return _grant_from_row(row)

    async def renew_in_transaction(
        self,
        session: AsyncSession,
        grant: LeaseGrant,
        *,
        lease_seconds: float,
    ) -> LeaseGrant | None:
        """在调用方事务中续期仍有效且未撤销的 grant。"""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        row = await session.scalar(
            update(ExecutionLeaseModel)
            .where(
                ExecutionLeaseModel.resource_type == grant.key.resource_type,
                ExecutionLeaseModel.resource_id == grant.key.resource_id,
                ExecutionLeaseModel.owner_id == grant.owner_id,
                ExecutionLeaseModel.fencing_token == grant.fencing_token,
                ExecutionLeaseModel.state == "active",
                ExecutionLeaseModel.lease_until > func.clock_timestamp(),
            )
            .values(
                lease_until=func.clock_timestamp()
                + timedelta(seconds=lease_seconds),
                updated_at=func.clock_timestamp(),
            )
            .returning(ExecutionLeaseModel)
        )
        return None if row is None else _grant_from_row(row)

    async def assert_current_in_transaction(
        self, session: AsyncSession, grant: LeaseGrant
    ) -> None:
        """锁定并校验 grant，供业务写入在同一事务内受 fence 保护。"""
        row = await session.scalar(
            select(ExecutionLeaseModel)
            .where(
                ExecutionLeaseModel.resource_type == grant.key.resource_type,
                ExecutionLeaseModel.resource_id == grant.key.resource_id,
            )
            .with_for_update()
        )
        now = await session.scalar(select(func.clock_timestamp()))
        if (
            row is None
            or now is None
            or row.state != "active"
            or row.owner_id != grant.owner_id
            or row.fencing_token != grant.fencing_token
            or row.lease_until is None
            or row.lease_until <= now
        ):
            raise LeaseLostError(grant.key)

    async def finish_in_transaction(
        self, session: AsyncSession, grant: LeaseGrant
    ) -> bool:
        """在当前业务事务中释放仍有效的正常执行租约。"""
        result = await session.execute(
            update(ExecutionLeaseModel)
            .where(
                ExecutionLeaseModel.resource_type == grant.key.resource_type,
                ExecutionLeaseModel.resource_id == grant.key.resource_id,
                ExecutionLeaseModel.owner_id == grant.owner_id,
                ExecutionLeaseModel.fencing_token == grant.fencing_token,
                ExecutionLeaseModel.state == "active",
                ExecutionLeaseModel.lease_until > func.clock_timestamp(),
            )
            .values(
                owner_id=None,
                state="released",
                lease_until=None,
                updated_at=func.clock_timestamp(),
            )
        )
        return bool(result.rowcount)

    async def revoke_in_transaction(
        self,
        session: AsyncSession,
        key: LeaseKey,
        *,
        expected_token: int,
    ) -> bool:
        """撤销指定代次并保留其 owner 和期限用于停止收敛。"""
        result = await session.execute(
            update(ExecutionLeaseModel)
            .where(
                ExecutionLeaseModel.resource_type == key.resource_type,
                ExecutionLeaseModel.resource_id == key.resource_id,
                ExecutionLeaseModel.fencing_token == expected_token,
                ExecutionLeaseModel.state == "active",
            )
            .values(state="revoked", updated_at=func.clock_timestamp())
        )
        return bool(result.rowcount)

    async def acknowledge_stopped_in_transaction(
        self, session: AsyncSession, grant: LeaseGrant
    ) -> bool:
        """接受仍为当前代次的退出确认并释放租约记录。"""
        result = await session.execute(
            update(ExecutionLeaseModel)
            .where(
                ExecutionLeaseModel.resource_type == grant.key.resource_type,
                ExecutionLeaseModel.resource_id == grant.key.resource_id,
                ExecutionLeaseModel.owner_id == grant.owner_id,
                ExecutionLeaseModel.fencing_token == grant.fencing_token,
                ExecutionLeaseModel.state.in_(("active", "revoked")),
            )
            .values(
                owner_id=None,
                state="released",
                lease_until=None,
                updated_at=func.clock_timestamp(),
            )
        )
        return bool(result.rowcount)

    async def renew(
        self, grant: LeaseGrant, *, lease_seconds: float
    ) -> LeaseGrant | None:
        """在独立短事务中续租，供通用心跳循环调用。"""
        async with self._session_factory() as session:
            async with session.begin():
                return await self.renew_in_transaction(
                    session, grant, lease_seconds=lease_seconds
                )

    async def assert_current(self, grant: LeaseGrant) -> None:
        """在独立事务中确认 grant 当前有效。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self.assert_current_in_transaction(session, grant)

    async def finish(self, grant: LeaseGrant) -> bool:
        """在独立事务中结束正常执行并释放租约。"""
        async with self._session_factory() as session:
            async with session.begin():
                return await self.finish_in_transaction(session, grant)

    async def revoke(self, key: LeaseKey, *, expected_token: int) -> bool:
        """在独立事务中撤销指定资源的当前 fencing 代次。"""
        async with self._session_factory() as session:
            async with session.begin():
                return await self.revoke_in_transaction(
                    session, key, expected_token=expected_token
                )

    async def acknowledge_stopped(self, grant: LeaseGrant) -> bool:
        """在独立事务中接受当前代次的退出确认。"""
        async with self._session_factory() as session:
            async with session.begin():
                return await self.acknowledge_stopped_in_transaction(
                    session, grant
                )


def _grant_from_row(row: ExecutionLeaseModel) -> LeaseGrant:
    """将 ORM 租约记录投影为不依赖 SQLAlchemy 的 grant。"""
    if row.owner_id is None or row.lease_until is None:
        raise RuntimeError("active_lease_expiry_missing")
    return LeaseGrant(
        key=LeaseKey(row.resource_type, row.resource_id),
        owner_id=row.owner_id,
        fencing_token=row.fencing_token,
        lease_until=row.lease_until,
    )


__all__ = ["PostgresLeaseCenter"]
