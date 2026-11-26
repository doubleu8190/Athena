"""系统提示词版本的事务仓储及 Run 快照读取。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from adapter.auth.repository.transaction_guard import verify_active_session_user
from adapter.system_prompt.converter import system_prompt_version_to_domain
from adapter.system_prompt.model import (
    SystemPromptSettingModel,
    SystemPromptVersionModel,
)
from common.ids import new_id
from domain.auth import AuthContext, UserRole
from domain.system_prompt import (
    ResolvedRunSystemPrompts,
    SystemPromptAuthorizationError,
    SystemPromptConflictError,
    SystemPromptKind,
    SystemPromptNotFoundError,
    SystemPromptRepository,
    SystemPromptUnavailableError,
    SystemPromptVersion,
    SystemPromptVersionPage,
)


async def current_system_prompts(
    session: AsyncSession,
) -> ResolvedRunSystemPrompts | None:
    """在调用方事务内用一条 SQL 读取两类当前指针及正文。"""
    main_setting = aliased(SystemPromptSettingModel)
    compression_setting = aliased(SystemPromptSettingModel)
    main_version = aliased(SystemPromptVersionModel)
    compression_version = aliased(SystemPromptVersionModel)
    # 两个指针属于独立行，但必须在同一数据库语句的快照中读取，避免 Run 混用跨请求状态。
    row = (
        await session.execute(
            select(main_version, compression_version)
            .select_from(main_setting)
            .join(
                main_version,
                and_(
                    main_version.id == main_setting.current_version_id,
                    main_version.kind == main_setting.kind,
                ),
            )
            .join(compression_setting, true())
            .join(
                compression_version,
                and_(
                    compression_version.id == compression_setting.current_version_id,
                    compression_version.kind == compression_setting.kind,
                ),
            )
            .where(
                main_setting.kind == SystemPromptKind.MAIN.value,
                compression_setting.kind == SystemPromptKind.COMPRESSION.value,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return ResolvedRunSystemPrompts(
        main=system_prompt_version_to_domain(row[0]),
        compression=system_prompt_version_to_domain(row[1]),
    )


class SqlAlchemySystemPromptRepository(SystemPromptRepository):
    """在 PostgreSQL 事务内比较并切换每类唯一当前指针。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """保存共享数据库会话工厂。"""
        self._session_factory = session_factory

    async def get_current(self, kind: SystemPromptKind) -> SystemPromptVersion | None:
        """通过当前指针读取指定类别正文。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SystemPromptVersionModel)
                .join(
                    SystemPromptSettingModel,
                    and_(
                        SystemPromptSettingModel.current_version_id
                        == SystemPromptVersionModel.id,
                        SystemPromptSettingModel.kind == SystemPromptVersionModel.kind,
                    ),
                )
                .where(SystemPromptSettingModel.kind == kind.value)
            )
        return None if row is None else system_prompt_version_to_domain(row)

    async def list_versions(
        self,
        kind: SystemPromptKind,
        *,
        page: int,
        page_size: int,
    ) -> SystemPromptVersionPage:
        """按时间和版本标识稳定倒序读取一页历史。"""
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError("system_prompt_page_invalid")
        async with self._session_factory() as session:
            total = (
                await session.scalar(
                    select(func.count(SystemPromptVersionModel.id)).where(
                        SystemPromptVersionModel.kind == kind.value
                    )
                )
                or 0
            )
            rows = list(
                (
                    await session.scalars(
                        select(SystemPromptVersionModel)
                        .where(SystemPromptVersionModel.kind == kind.value)
                        .order_by(
                            SystemPromptVersionModel.created_at.desc(),
                            SystemPromptVersionModel.id.desc(),
                        )
                        .offset((page - 1) * page_size)
                        .limit(page_size)
                    )
                ).all()
            )
        return SystemPromptVersionPage(
            items=tuple(system_prompt_version_to_domain(row) for row in rows),
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_version(
        self, kind: SystemPromptKind, version_id: str
    ) -> SystemPromptVersion | None:
        """读取请求类别下的指定历史版本。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SystemPromptVersionModel).where(
                    SystemPromptVersionModel.id == version_id,
                    SystemPromptVersionModel.kind == kind.value,
                )
            )
        return None if row is None else system_prompt_version_to_domain(row)

    async def save(
        self,
        kind: SystemPromptKind,
        *,
        content: str,
        expected_current_version_id: str,
        actor: AuthContext,
        created_at: datetime,
    ) -> SystemPromptVersion:
        """锁定当前指针，在版本插入与指针切换同一事务内提交。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_admin(session, actor)
                setting, current = await self._lock_current(session, kind)
                if setting.current_version_id != expected_current_version_id:
                    raise SystemPromptConflictError()
                if current.content == content:
                    return system_prompt_version_to_domain(current)
                return await self._insert_and_switch(
                    session,
                    setting,
                    kind,
                    content,
                    actor.user.id,
                    created_at,
                    restored_from_version_id=None,
                )

    async def restore(
        self,
        kind: SystemPromptKind,
        *,
        source_version_id: str,
        expected_current_version_id: str,
        actor: AuthContext,
        created_at: datetime,
    ) -> SystemPromptVersion:
        """恢复历史正文，但始终生成新版本并保留来源追溯。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_admin(session, actor)
                setting, current = await self._lock_current(session, kind)
                if setting.current_version_id != expected_current_version_id:
                    raise SystemPromptConflictError()
                source = await session.scalar(
                    select(SystemPromptVersionModel).where(
                        SystemPromptVersionModel.id == source_version_id,
                        SystemPromptVersionModel.kind == kind.value,
                    )
                )
                if source is None:
                    raise SystemPromptNotFoundError()
                return await self._insert_and_switch(
                    session,
                    setting,
                    kind,
                    source.content,
                    actor.user.id,
                    created_at,
                    restored_from_version_id=source.id,
                )

    async def get_by_id(self, version_id: str) -> SystemPromptVersion | None:
        """通过不可变标识读取任意类别版本。"""
        async with self._session_factory() as session:
            row = await session.get(SystemPromptVersionModel, version_id)
        return None if row is None else system_prompt_version_to_domain(row)

    async def get_pair_by_ids(
        self, main_version_id: str, compression_version_id: str
    ) -> tuple[SystemPromptVersion, SystemPromptVersion] | None:
        """用一条 SQL 解析 Run 固定的主提示词与压缩提示词正文。"""
        main_version = aliased(SystemPromptVersionModel)
        compression_version = aliased(SystemPromptVersionModel)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(main_version, compression_version)
                    .select_from(main_version)
                    .join(compression_version, true())
                    .where(
                        main_version.id == main_version_id,
                        compression_version.id == compression_version_id,
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        main = system_prompt_version_to_domain(row[0])
        compression = system_prompt_version_to_domain(row[1])
        if (
            main.kind is not SystemPromptKind.MAIN
            or compression.kind is not SystemPromptKind.COMPRESSION
        ):
            return None
        return main, compression

    @staticmethod
    async def _require_admin(session: AsyncSession, actor: AuthContext) -> None:
        """在版本写入事务内以无锁单条 JOIN 复核当前管理员身份。"""
        user = await verify_active_session_user(session, actor.user.id, actor.session_id)
        if user is None or user.role is not UserRole.SUPER_ADMIN:
            raise SystemPromptAuthorizationError()

    @staticmethod
    async def _lock_current(
        session: AsyncSession, kind: SystemPromptKind
    ) -> tuple[SystemPromptSettingModel, SystemPromptVersionModel]:
        """锁定类别指针并读取其正文，确保 CAS 与切换原子执行。"""
        setting = await session.scalar(
            select(SystemPromptSettingModel)
            .where(SystemPromptSettingModel.kind == kind.value)
            .with_for_update()
        )
        if setting is None:
            raise SystemPromptUnavailableError()
        current = await session.scalar(
            select(SystemPromptVersionModel).where(
                SystemPromptVersionModel.id == setting.current_version_id,
                SystemPromptVersionModel.kind == kind.value,
            )
        )
        if current is None:
            raise SystemPromptUnavailableError()
        return setting, current

    @staticmethod
    async def _insert_and_switch(
        session: AsyncSession,
        setting: SystemPromptSettingModel,
        kind: SystemPromptKind,
        content: str,
        created_by: str,
        created_at: datetime,
        *,
        restored_from_version_id: str | None,
    ) -> SystemPromptVersion:
        """在同一事务内插入不可变版本并切换当前指针。"""
        row = SystemPromptVersionModel(
            id=new_id("sysp"),
            kind=kind.value,
            content=content,
            created_by=created_by,
            created_at=created_at,
            restored_from_version_id=restored_from_version_id,
        )
        session.add(row)
        # 复合外键要求版本行先存在，再切换当前指针；两次 flush 仍处于同一事务。
        await session.flush()
        setting.current_version_id = row.id
        await session.flush()
        return system_prompt_version_to_domain(row)

__all__ = ["SqlAlchemySystemPromptRepository", "current_system_prompts"]
