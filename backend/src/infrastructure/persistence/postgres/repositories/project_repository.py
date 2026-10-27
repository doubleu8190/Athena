"""PostgreSQL 显式项目记忆空间目录仓储。"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import cast, delete as sql_delete, func, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.memory import MemoryRecordMetadata
from domain.projects import Project, ProjectRepositoryPort
from domain.memory.suppression import (
    EPISODE_SUPPRESSION_FINGERPRINT,
    PROJECT_SUPPRESSION_FINGERPRINT,
    PROJECT_TURN_SUPPRESSION_FINGERPRINT,
    source_suppression_lock_id,
)

from ..models import (
    AgentCommandModel,
    AgentRunModel,
    MemoryEpisodeModel,
    MemoryEpisodeProjectBindingModel,
    MemoryExtractionSnapshotModel,
    MemoryJobScopeSnapshotModel,
    MemoryModel,
    MemoryProcessingJobModel,
    ProjectModel,
    MemoryRunEvidenceSnapshotModel,
    MemorySourceSuppressionModel,
    MemoryWriteOperationModel,
    SessionModel,
)


def _source_turn_ids(metadata: object) -> set[str]:
    """Read turn identities from a project memory without retaining its text."""
    if not isinstance(metadata, dict):
        return set()
    result = {
        value
        for value in (metadata.get("source_turn_id"),)
        if isinstance(value, str) and value
    }
    raw_fact = metadata.get("fact")
    if raw_fact is not None:
        try:
            fact = MemoryRecordMetadata.model_validate({"fact": raw_fact}).fact
        except (TypeError, ValueError):
            fact = None
        if fact is not None:
            result.update(
                source.source_id
                for source in fact.source_refs
                if source.source_type == "turn"
            )
    return result


class PostgresProjectRepository(ProjectRepositoryPort):
    """只管理用户明确创建的项目空间名称与稳定 ID。"""

    def __init__(
        self,
        session_factory: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ],
    ) -> None:
        self._session_factory = session_factory

    async def create(self, project: Project) -> Project:
        """保存新项目空间。

        参数：project 是服务端生成 ID 且名称已规范化的项目空间。
        返回：持久化的同一项目空间。
        异常：ID 冲突或数据库错误向调用方传播。
        """
        async with self._session_factory() as session:
            async with session.begin():
                session.add(
                    ProjectModel(
                        id=project.id,
                        name=project.name,
                        created_at=project.created_at,
                        updated_at=project.updated_at,
                    )
                )
        return project

    async def get(self, project_id: str) -> Project | None:
        """按稳定 ID 读取项目空间。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ProjectModel).where(
                    ProjectModel.id == project_id,
                    ProjectModel.deleted_time.is_(None),
                )
            )
        return None if row is None else _to_domain(row)

    async def list(self) -> list[Project]:
        """按最近更新时间倒序列出显式项目空间。"""
        async with self._session_factory() as session:
            activity = (
                select(
                    SessionModel.project_id.label("project_id"),
                    func.max(SessionModel.last_activity_at).label("last_activity_at"),
                )
                .where(
                    SessionModel.deleted_time.is_(None),
                    SessionModel.project_id.is_not(None),
                )
                .group_by(SessionModel.project_id)
                .subquery()
            )
            rows = (
                await session.scalars(
                    select(ProjectModel)
                    .outerjoin(activity, activity.c.project_id == ProjectModel.id)
                    .where(ProjectModel.deleted_time.is_(None))
                    .order_by(
                        func.coalesce(
                            activity.c.last_activity_at, ProjectModel.created_at
                        ).desc(),
                        ProjectModel.id,
                    )
                )
            ).all()
        return [_to_domain(row) for row in rows]

    async def rename(self, project_id: str, name: str) -> Project | None:
        """只更新显示名称，不改变项目 ID 和聊天活动时间。"""
        now = datetime.now(timezone.utc).isoformat()
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(ProjectModel)
                    .where(
                        ProjectModel.id == project_id,
                        ProjectModel.deleted_time.is_(None),
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                row.name = name
                row.updated_at = now
                await session.flush()
                return _to_domain(row)

    async def delete(self, project_id: str) -> bool:
        """清理项目记忆并软删除项目下的聊天；活动 Run 会拒绝删除。"""
        now = datetime.now(timezone.utc).isoformat()
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            source_suppression_lock_id(
                                project_id, PROJECT_SUPPRESSION_FINGERPRINT
                            )
                        )
                    )
                )
                project = await session.scalar(
                    select(ProjectModel)
                    .where(
                        ProjectModel.id == project_id,
                        ProjectModel.deleted_time.is_(None),
                    )
                    .with_for_update()
                )
                if project is None:
                    return False

                session_ids = list((await session.scalars(
                    select(SessionModel.id).where(
                        SessionModel.project_id == project_id,
                        SessionModel.deleted_time.is_(None),
                    ).with_for_update()
                )).all())
                active_run = (
                    await session.scalar(
                        select(AgentRunModel.run_id)
                        .where(
                            AgentRunModel.session_id.in_(session_ids),
                            AgentRunModel.status.notin_(
                                ("completed", "failed", "cancelled")
                            ),
                        )
                        .limit(1)
                    )
                    if session_ids
                    else None
                )
                active_command = (
                    await session.scalar(
                        select(AgentCommandModel.command_id)
                        .where(
                            AgentCommandModel.session_id.in_(session_ids),
                            AgentCommandModel.status.in_(("queued", "running")),
                        )
                        .limit(1)
                    )
                    if session_ids
                    else None
                )
                if active_run is not None or active_command is not None:
                    raise ValueError("project_busy")

                await session.execute(
                    pg_insert(MemorySourceSuppressionModel)
                    .values(
                        source_type="project",
                        source_id=project_id,
                        item_fingerprint=PROJECT_SUPPRESSION_FINGERPRINT,
                        created_at=now,
                    )
                    .on_conflict_do_nothing()
                )

                metadata = cast(MemoryModel.metadata_json, JSONB)
                fact = metadata["fact"]
                project_memory_scope = (
                    (fact["scope_kind"].astext == "project")
                    & (fact["scope_id"].astext == project_id)
                ) | (
                    fact.astext.is_(None)
                    & (metadata["scope_kind"].astext == "project")
                    & (metadata["scope_id"].astext == project_id)
                )
                project_rows = (
                    await session.scalars(
                        select(MemoryModel)
                        .where(
                            project_memory_scope,
                            MemoryModel.deleted_time.is_(None),
                        )
                        .with_for_update()
                    )
                ).all()
                turn_ids: set[str] = set()
                for memory in project_rows:
                    raw_metadata = memory.metadata_json
                    if isinstance(raw_metadata, str):
                        try:
                            raw_metadata = json.loads(raw_metadata)
                        except (TypeError, ValueError):
                            raw_metadata = {}
                    turn_ids.update(_source_turn_ids(raw_metadata))
                    memory.status = "deleted"
                    memory.deleted_time = now

                project_episode_rows = (
                    await session.execute(
                        select(
                            MemoryEpisodeModel.episode_id,
                            MemoryEpisodeModel.turn_id,
                        )
                        .join(
                            MemoryEpisodeProjectBindingModel,
                            MemoryEpisodeProjectBindingModel.episode_id
                            == MemoryEpisodeModel.episode_id,
                        )
                        .where(
                            MemoryEpisodeProjectBindingModel.project_id == project_id
                        )
                    )
                ).all()
                episode_ids = [episode_id for episode_id, _ in project_episode_rows]
                turn_ids.update(turn_id for _, turn_id in project_episode_rows)

                if session_ids:
                    turn_ids.update(
                        (
                            await session.scalars(
                                select(AgentRunModel.run_id).where(
                                    AgentRunModel.session_id.in_(session_ids)
                                )
                            )
                        ).all()
                    )

                scope_turn_ids = list(
                    (
                        await session.scalars(
                            select(MemoryJobScopeSnapshotModel.turn_id).where(
                                MemoryJobScopeSnapshotModel.project_id == project_id
                            )
                        )
                    ).all()
                )
                turn_ids.update(scope_turn_ids)
                for turn_id in sorted(turn_ids):
                    await session.execute(
                        select(
                            func.pg_advisory_xact_lock(
                                source_suppression_lock_id(
                                    turn_id, PROJECT_TURN_SUPPRESSION_FINGERPRINT
                                )
                            )
                        )
                    )
                    await session.execute(
                        pg_insert(MemorySourceSuppressionModel)
                        .values(
                            source_type="project_turn",
                            source_id=turn_id,
                            item_fingerprint=PROJECT_TURN_SUPPRESSION_FINGERPRINT,
                            created_at=now,
                        )
                        .on_conflict_do_nothing()
                    )
                for _, turn_id in project_episode_rows:
                    await session.execute(
                        pg_insert(MemorySourceSuppressionModel)
                        .values(
                            source_type="episode_turn",
                            source_id=turn_id,
                            item_fingerprint=EPISODE_SUPPRESSION_FINGERPRINT,
                            created_at=now,
                        )
                        .on_conflict_do_nothing()
                    )

                if turn_ids:
                    turn_filter = MemoryProcessingJobModel.turn_id.in_(turn_ids)
                    await session.execute(
                        sql_delete(MemoryProcessingJobModel).where(turn_filter)
                    )
                    await session.execute(
                        sql_delete(MemoryJobScopeSnapshotModel).where(
                            MemoryJobScopeSnapshotModel.turn_id.in_(turn_ids)
                        )
                    )
                    await session.execute(
                        sql_delete(MemoryExtractionSnapshotModel).where(
                            MemoryExtractionSnapshotModel.turn_id.in_(turn_ids)
                        )
                    )
                    await session.execute(
                        sql_delete(MemoryRunEvidenceSnapshotModel).where(
                            MemoryRunEvidenceSnapshotModel.turn_id.in_(turn_ids)
                        )
                    )
                    await session.execute(
                        sql_delete(MemoryWriteOperationModel).where(
                            MemoryWriteOperationModel.turn_id.in_(turn_ids)
                        )
                    )
                if episode_ids:
                    await session.execute(
                        sql_delete(MemoryEpisodeModel).where(
                            MemoryEpisodeModel.episode_id.in_(episode_ids)
                        )
                    )
                await session.execute(
                    update(SessionModel)
                    .where(
                        SessionModel.project_id == project_id,
                        SessionModel.deleted_time.is_(None),
                    )
                    .values(deleted_time=now)
                )
                project.deleted_time = now
                project.updated_at = now
        return True


def _to_domain(row: ProjectModel) -> Project:
    return Project(
        id=row.id,
        name=row.name,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_time=row.deleted_time,
    )


__all__ = ["PostgresProjectRepository"]
