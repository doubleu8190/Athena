"""个人工作偏好与显式记忆的 async SQLAlchemy 仓储实现。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.auth.model import UserModel
from adapter.preference.converter import memory_to_domain, profile_to_domain
from adapter.preference.model import UserMemoryModel, UserWorkProfileModel
from domain.preference import (
    PreferenceContext,
    PreferenceError,
    PreferenceRepository,
    UserMemory,
    UserMemoryDisabledError,
    UserMemoryLimitReachedError,
    UserWorkProfile,
)


class SqlAlchemyPreferenceRepository(PreferenceRepository):
    """基于 PostgreSQL 行锁与事务保障配额与开关一致性的偏好仓储。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """保存异步数据库会话工厂。"""
        self._session_factory = session_factory

    async def get_profile(self, user_id: str) -> UserWorkProfile | None:
        """读取用户个人工作偏好；未创建记录时返回 None。"""
        async with self._session_factory() as session:
            row = await session.get(UserWorkProfileModel, user_id)
        return None if row is None else profile_to_domain(row)

    async def upsert_profile(
        self,
        user_id: str,
        *,
        custom_instructions: str,
        memory_enabled: bool,
        updated_at: datetime,
    ) -> UserWorkProfile:
        """在同一事务内锁定当前用户行后创建或更新个人工作偏好。"""
        async with self._session_factory() as session:
            async with session.begin():
                # 锁定 users 行，串行化同一用户的偏好 upsert 与显式记忆并发写入。
                locked_user_id = await session.scalar(
                    select(UserModel.id).where(UserModel.id == user_id).with_for_update()
                )
                if locked_user_id is None:
                    raise PreferenceError("user_not_found")
                row = await session.get(UserWorkProfileModel, user_id)
                if row is None:
                    row = UserWorkProfileModel(
                        user_id=user_id,
                        custom_instructions=custom_instructions,
                        memory_enabled=memory_enabled,
                        updated_at=updated_at,
                    )
                    session.add(row)
                else:
                    row.custom_instructions = custom_instructions
                    row.memory_enabled = memory_enabled
                    row.updated_at = updated_at
                await session.flush()
                result = profile_to_domain(row)
        return result

    async def list_memories(self, user_id: str) -> tuple[UserMemory, ...]:
        """按 updated_at DESC, id DESC 稳定顺序读取当前用户全部显式记忆。"""
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(UserMemoryModel)
                    .where(UserMemoryModel.user_id == user_id)
                    .order_by(UserMemoryModel.updated_at.desc(), UserMemoryModel.id.desc())
                )
            ).all()
        return tuple(memory_to_domain(row) for row in rows)

    async def create_memory(
        self,
        memory: UserMemory,
        *,
        max_memories: int,
        require_memory_enabled: bool = False,
    ) -> UserMemory:
        """在事务内锁定当前用户行再校验开关与 50 条硬上限，防止并发突破配额。"""
        async with self._session_factory() as session:
            async with session.begin():
                # 锁定当前用户行（SELECT ... FROM users WHERE id = :user_id FOR UPDATE），
                # 串行化同一用户的并发新增记忆请求，杜绝超额写入。
                locked_user_id = await session.scalar(
                    select(UserModel.id).where(UserModel.id == memory.user_id).with_for_update()
                )
                if locked_user_id is None:
                    raise PreferenceError("user_not_found")
                if require_memory_enabled:
                    # 对话工具写入需在用户锁内复核开关，避免用户刚关闭开关仍写入记忆。
                    profile = await session.get(UserWorkProfileModel, memory.user_id)
                    if profile is not None and not profile.memory_enabled:
                        raise UserMemoryDisabledError("user_memory_disabled")
                current_count = int(
                    (
                        await session.scalar(
                            select(func.count(UserMemoryModel.id)).where(
                                UserMemoryModel.user_id == memory.user_id
                            )
                        )
                    )
                    or 0
                )
                if current_count >= max_memories:
                    raise UserMemoryLimitReachedError("user_memory_limit_reached")
                row = UserMemoryModel(
                    id=memory.id,
                    user_id=memory.user_id,
                    content=memory.content,
                    source=memory.source.value,
                    created_at=memory.created_at,
                    updated_at=memory.updated_at,
                )
                session.add(row)
                await session.flush()
                result = memory_to_domain(row)
        return result

    async def update_memory(
        self,
        user_id: str,
        memory_id: str,
        *,
        content: str,
        updated_at: datetime,
    ) -> UserMemory | None:
        """按 (id, user_id) 锁定并更新记忆正文，跨用户访问按不存在返回 None。"""
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(UserMemoryModel)
                    .where(
                        UserMemoryModel.id == memory_id,
                        UserMemoryModel.user_id == user_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                row.content = content
                row.updated_at = updated_at
                await session.flush()
                result = memory_to_domain(row)
        return result

    async def delete_memory(self, user_id: str, memory_id: str) -> bool:
        """按 (id, user_id) 锁定并删除单条记忆，跨用户访问按不存在返回 False。"""
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(UserMemoryModel)
                    .where(
                        UserMemoryModel.id == memory_id,
                        UserMemoryModel.user_id == user_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    return False
                await session.delete(row)
                await session.flush()
        return True

    async def get_context(self, user_id: str) -> PreferenceContext:
        """在单次只读会话内读取个人偏好与（开启时的）显式记忆列表。"""
        async with self._session_factory() as session:
            profile_row = await session.get(UserWorkProfileModel, user_id)
            custom_instructions = "" if profile_row is None else profile_row.custom_instructions
            memory_enabled = True if profile_row is None else profile_row.memory_enabled
            if not memory_enabled:
                return PreferenceContext(
                    custom_instructions=custom_instructions,
                    memory_enabled=False,
                    memories=(),
                )
            rows = (
                await session.scalars(
                    select(UserMemoryModel)
                    .where(UserMemoryModel.user_id == user_id)
                    .order_by(UserMemoryModel.updated_at.desc(), UserMemoryModel.id.desc())
                )
            ).all()
        return PreferenceContext(
            custom_instructions=custom_instructions,
            memory_enabled=True,
            memories=tuple(memory_to_domain(row) for row in rows),
        )


__all__ = ["SqlAlchemyPreferenceRepository"]
