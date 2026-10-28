"""PostgreSQL 租约中心和可复用的事务内 fencing 操作。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone

from sqlalchemy import event, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.schema import DDL
from sqlalchemy.ext.asyncio import AsyncSession

from domain.leases import LeaseGrant, LeaseKey, LeaseLostError, LeaseRecord
from ..models import Base, ExecutionLeaseModel

_CREATE_LEASE_ENQUEUE_FUNCTION = DDL("""
CREATE OR REPLACE FUNCTION ensure_execution_lease_for_work() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    lease_resource_id text;
BEGIN
    lease_resource_id := to_jsonb(NEW) ->> TG_ARGV[1];
    IF lease_resource_id IS NOT NULL THEN
        INSERT INTO execution_leases (
            resource_type, resource_id, owner_id, fencing_token, state,
            lease_until, created_at, updated_at
        ) VALUES (
            TG_ARGV[0], lease_resource_id, NULL, NULL, 'pending', NULL,
            clock_timestamp(), clock_timestamp()
        ) ON CONFLICT (resource_type, resource_id) DO NOTHING;
    END IF;
    RETURN NEW;
END;
$$;
""").execute_if(dialect="postgresql")


_INSTALL_LEASE_ENQUEUE_TRIGGERS = DDL("""
DO $$
DECLARE
    mapping text[];
    table_name text;
    resource_type text;
    id_column text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = current_schema() AND tablename = 'execution_leases') THEN
        FOREACH mapping SLICE 1 IN ARRAY ARRAY[
            ARRAY['agent_runs', 'root_execution', 'run_id'],
            ARRAY['task_executions', 'worker_execution', 'execution_id'],
            ARRAY['root_resume_jobs', 'root_resume', 'job_id'],
            ARRAY['orchestration_outbox', 'orchestration_outbox', 'message_id'],
            ARRAY['knowledge_document_jobs', 'document_ingestion', 'attachment_id'],
            ARRAY['memory_processing_jobs', 'memory_processing', 'turn_id']
        ] LOOP
            table_name := mapping[1];
            resource_type := mapping[2];
            id_column := mapping[3];
            IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = current_schema() AND tablename = mapping[1]) THEN
                EXECUTE format(
                    'DROP TRIGGER IF EXISTS execution_lease_enqueue ON %%I',
                    table_name
                );
                EXECUTE format(
                    'CREATE TRIGGER execution_lease_enqueue AFTER INSERT ON %%I '
                    'FOR EACH ROW EXECUTE FUNCTION ensure_execution_lease_for_work(%%L, %%L)',
                    table_name, resource_type, id_column
                );
            END IF;
        END LOOP;
    END IF;
END;
$$;
""").execute_if(dialect="postgresql")

event.listen(Base.metadata, "after_create", _CREATE_LEASE_ENQUEUE_FUNCTION)
event.listen(Base.metadata, "after_create", _INSTALL_LEASE_ENQUEUE_TRIGGERS)


class PostgresLeaseCenter:
    """为不同队列提供同一套租约状态转换。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        """保存用于心跳续租的 PostgreSQL 会话工厂。"""
        self._session_factory = session_factory

    async def ensure_in_transaction(self, session: AsyncSession, key: LeaseKey) -> None:
        """在入队业务事务中预建空闲租约行，重复调用保持幂等。"""
        await session.execute(
            pg_insert(ExecutionLeaseModel)
            .values(
                resource_type=key.resource_type,
                resource_id=key.resource_id,
                owner_id=None,
                fencing_token=None,
                state="pending",
                lease_until=None,
                created_at=func.clock_timestamp(),
                updated_at=func.clock_timestamp(),
            )
            .on_conflict_do_nothing(index_elements=["resource_type", "resource_id"])
        )

    async def schedule_in_transaction(
        self, session: AsyncSession, key: LeaseKey
    ) -> None:
        """让已结束资源重新进入待领取状态，同时保留 fencing token。"""
        await self.ensure_in_transaction(session, key)
        await session.execute(
            update(ExecutionLeaseModel)
            .where(
                ExecutionLeaseModel.resource_type == key.resource_type,
                ExecutionLeaseModel.resource_id == key.resource_id,
                ExecutionLeaseModel.state == "completed",
            )
            .values(
                state="pending",
                owner_id=None,
                lease_until=None,
                updated_at=func.clock_timestamp(),
            )
        )

    async def complete_unclaimed_in_transaction(
        self, session: AsyncSession, key: LeaseKey
    ) -> bool:
        """永久关闭尚未领取或已释放的资源。"""
        result = await session.execute(
            update(ExecutionLeaseModel)
            .where(
                ExecutionLeaseModel.resource_type == key.resource_type,
                ExecutionLeaseModel.resource_id == key.resource_id,
                or_(
                    ExecutionLeaseModel.state.in_(("pending", "released")),
                    (ExecutionLeaseModel.state.in_(("active", "revoked")))
                    & (ExecutionLeaseModel.lease_until <= func.clock_timestamp()),
                ),
            )
            .values(
                state="completed",
                owner_id=None,
                lease_until=None,
                updated_at=func.clock_timestamp(),
            )
        )
        return bool(result.rowcount)

    async def claimable_keys_in_transaction(
        self,
        session: AsyncSession,
        resource_type: str,
        *,
        limit: int,
        after_resource_id: str | None = None,
    ) -> list[LeaseKey]:
        """按资源 ID 分页列出租约中心中的可领取资源。"""
        if limit < 1:
            raise ValueError("lease candidate limit must be positive")
        statement = select(ExecutionLeaseModel.resource_id).where(
            ExecutionLeaseModel.resource_type == resource_type,
            or_(
                ExecutionLeaseModel.state.in_(("pending", "released")),
                (ExecutionLeaseModel.state.in_(("active", "revoked")))
                & (ExecutionLeaseModel.lease_until <= func.clock_timestamp()),
            ),
        )
        if after_resource_id is not None:
            statement = statement.where(
                ExecutionLeaseModel.resource_id > after_resource_id
            )
        rows = (
            await session.scalars(
                statement.order_by(ExecutionLeaseModel.resource_id).limit(limit)
            )
        ).all()
        return [LeaseKey(resource_type, resource_id) for resource_id in rows]

    async def expired_keys_in_transaction(
        self, session: AsyncSession, resource_type: str, *, limit: int
    ) -> list[LeaseKey]:
        """只列出数据库时钟已确认过期的活跃或撤销授权。"""
        if limit < 1:
            raise ValueError("lease candidate limit must be positive")
        rows = (
            await session.scalars(
                select(ExecutionLeaseModel.resource_id)
                .where(
                    ExecutionLeaseModel.resource_type == resource_type,
                    ExecutionLeaseModel.state.in_(("active", "revoked")),
                    ExecutionLeaseModel.lease_until <= func.clock_timestamp(),
                )
                .order_by(
                    ExecutionLeaseModel.lease_until, ExecutionLeaseModel.resource_id
                )
                .limit(limit)
            )
        ).all()
        return [LeaseKey(resource_type, resource_id) for resource_id in rows]

    async def claimable_keys(
        self,
        resource_type: str,
        *,
        limit: int,
        after_resource_id: str | None = None,
    ) -> list[LeaseKey]:
        """在独立只读事务中查询可领取资源身份。"""
        async with self._session_factory() as session:
            return await self.claimable_keys_in_transaction(
                session,
                resource_type,
                limit=limit,
                after_resource_id=after_resource_id,
            )

    async def get_in_transaction(
        self, session: AsyncSession, key: LeaseKey
    ) -> LeaseRecord | None:
        """读取一条租约的领域投影，不把 ORM 模型交给业务仓储。"""
        row = await session.get(
            ExecutionLeaseModel, (key.resource_type, key.resource_id)
        )
        return None if row is None else _record_from_row(row)

    async def get(self, key: LeaseKey) -> LeaseRecord | None:
        """在独立会话中读取资源租约状态。"""
        async with self._session_factory() as session:
            return await self.get_in_transaction(session, key)

    async def get_many_in_transaction(
        self, session: AsyncSession, resource_type: str, resource_ids: list[str]
    ) -> dict[str, LeaseRecord]:
        """按资源 ID 批量读取租约领域投影。"""
        if not resource_ids:
            return {}
        rows = (
            await session.scalars(
                select(ExecutionLeaseModel).where(
                    ExecutionLeaseModel.resource_type == resource_type,
                    ExecutionLeaseModel.resource_id.in_(resource_ids),
                )
            )
        ).all()
        return {row.resource_id: _record_from_row(row) for row in rows}

    async def list_in_transaction(
        self, session: AsyncSession, resource_type: str
    ) -> list[LeaseRecord]:
        """列出一种资源类型的租约状态，供控制面按业务 ID 过滤。"""
        rows = (
            await session.scalars(
                select(ExecutionLeaseModel)
                .where(ExecutionLeaseModel.resource_type == resource_type)
                .order_by(ExecutionLeaseModel.resource_id)
            )
        ).all()
        return [_record_from_row(row) for row in rows]

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

        row = await session.scalar(
            update(ExecutionLeaseModel)
            .where(
                ExecutionLeaseModel.resource_type == key.resource_type,
                ExecutionLeaseModel.resource_id == key.resource_id,
                or_(
                    ExecutionLeaseModel.state.in_(("pending", "released")),
                    (ExecutionLeaseModel.state.in_(("active", "revoked")))
                    & (ExecutionLeaseModel.lease_until <= func.clock_timestamp()),
                ),
            )
            .values(
                owner_id=owner_id,
                fencing_token=func.coalesce(ExecutionLeaseModel.fencing_token, 0) + 1,
                state="active",
                lease_until=func.clock_timestamp() + timedelta(seconds=lease_seconds),
                updated_at=func.clock_timestamp(),
            )
            .returning(ExecutionLeaseModel)
        )
        return None if row is None else _grant_from_row(row)

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
                lease_until=func.clock_timestamp() + timedelta(seconds=lease_seconds),
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
        self, session: AsyncSession, grant: LeaseGrant, *, completed: bool = False
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
                state="completed" if completed else "released",
                lease_until=None,
                updated_at=func.clock_timestamp(),
            )
            .returning(ExecutionLeaseModel.resource_id)
        )
        return result.scalar_one_or_none() is not None

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
            .returning(ExecutionLeaseModel.resource_id)
        )
        return result.scalar_one_or_none() is not None

    async def acknowledge_stopped_in_transaction(
        self, session: AsyncSession, grant: LeaseGrant, *, completed: bool = False
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
                state="completed" if completed else "released",
                lease_until=None,
                updated_at=func.clock_timestamp(),
            )
            .returning(ExecutionLeaseModel.resource_id)
        )
        return result.scalar_one_or_none() is not None

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

    async def finish(self, grant: LeaseGrant, *, completed: bool = False) -> bool:
        """在独立事务中结束正常执行并释放租约。"""
        async with self._session_factory() as session:
            async with session.begin():
                return await self.finish_in_transaction(
                    session, grant, completed=completed
                )

    async def revoke(self, key: LeaseKey, *, expected_token: int) -> bool:
        """在独立事务中撤销指定资源的当前 fencing 代次。"""
        async with self._session_factory() as session:
            async with session.begin():
                return await self.revoke_in_transaction(
                    session, key, expected_token=expected_token
                )

    async def acknowledge_stopped(
        self, grant: LeaseGrant, *, completed: bool = False
    ) -> bool:
        """在独立事务中接受当前代次的退出确认。"""
        async with self._session_factory() as session:
            async with session.begin():
                return await self.acknowledge_stopped_in_transaction(
                    session, grant, completed=completed
                )


def _grant_from_row(row: ExecutionLeaseModel) -> LeaseGrant:
    """将 ORM 租约记录投影为不依赖 SQLAlchemy 的 grant。"""
    if row.owner_id is None or row.lease_until is None or row.fencing_token is None:
        raise RuntimeError("active_lease_expiry_missing")
    return LeaseGrant(
        key=LeaseKey(row.resource_type, row.resource_id),
        owner_id=row.owner_id,
        fencing_token=row.fencing_token,
        lease_until=row.lease_until,
    )


def _record_from_row(row: ExecutionLeaseModel) -> LeaseRecord:
    """把 ORM 行转换为租约中心公开的不可变领域状态。"""
    return LeaseRecord(
        key=LeaseKey(row.resource_type, row.resource_id),
        state=row.state,
        owner_id=row.owner_id,
        fencing_token=row.fencing_token,
        lease_until=row.lease_until,
    )


__all__ = ["PostgresLeaseCenter"]
