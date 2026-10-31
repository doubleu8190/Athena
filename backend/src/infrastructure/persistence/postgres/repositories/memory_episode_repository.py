"""PostgreSQL 任务回顾仓储。"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete as sql_delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from domain.memory import (
    MemoryEpisode,
    MemoryEpisodeRepositoryPort,
    MemoryEpisodeSearchRequest,
    MemorySourceReference,
)
from domain.leases import LeaseGrant, LeaseKey
from domain.memory.suppression import (
    EPISODE_SUPPRESSION_FINGERPRINT,
    PROJECT_SUPPRESSION_FINGERPRINT,
    PROJECT_TURN_SUPPRESSION_FINGERPRINT,
    source_suppression_lock_id,
)
from application.contracts.memory import (
    MemoryEpisodeSummarySchema,
    MemorySourceReferenceSchema,
    memory_domain_to_schema,
    memory_schema_to_domain,
)

from ..models import (
    AgentRunModel,
    MemoryEpisodeModel,
    MemorySourceSuppressionModel,
    MessageModel,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorTaskModel,
    SessionModel,
)
from .lease_center import PostgresLeaseCenter


def _now() -> datetime:
    """生成当前 UTC 时间。

    参数：无。
    返回：可直接写入 PostgreSQL 时间列的 UTC 时间。
    异常：不主动抛出业务异常。
    """
    return datetime.now(timezone.utc)


def _episode_suppression_lock_id(turn_id: str) -> int:
    """Match memory deletion's advisory lock for an episode derived from a turn."""
    return source_suppression_lock_id(turn_id, EPISODE_SUPPRESSION_FINGERPRINT)


async def _project_source_is_suppressed(
    session: AsyncSession, *, project_id: str | None, turn_id: str
) -> bool:
    """Lock and check both a deleted project and its frozen historical turns."""
    if project_id:
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    source_suppression_lock_id(
                        project_id, PROJECT_SUPPRESSION_FINGERPRINT
                    )
                )
            )
        )
        deleted_project = await session.scalar(
            select(MemorySourceSuppressionModel.source_id)
            .where(
                MemorySourceSuppressionModel.source_type == "project",
                MemorySourceSuppressionModel.source_id == project_id,
                MemorySourceSuppressionModel.item_fingerprint
                == PROJECT_SUPPRESSION_FINGERPRINT,
            )
            .limit(1)
        )
        if deleted_project is not None:
            return True
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                source_suppression_lock_id(
                    turn_id, PROJECT_TURN_SUPPRESSION_FINGERPRINT
                )
            )
        )
    )
    deleted_turn = await session.scalar(
        select(MemorySourceSuppressionModel.source_id)
        .where(
            MemorySourceSuppressionModel.source_type == "project_turn",
            MemorySourceSuppressionModel.source_id == turn_id,
            MemorySourceSuppressionModel.item_fingerprint
            == PROJECT_TURN_SUPPRESSION_FINGERPRINT,
        )
        .limit(1)
    )
    return deleted_turn is not None


class PostgresMemoryEpisodeRepository(MemoryEpisodeRepositoryPort):
    """以来源轮次幂等保存结构化任务回顾。"""

    def __init__(
        self,
        session_factory: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ],
        *,
        lease_center: PostgresLeaseCenter | None = None,
    ) -> None:
        """保存异步数据库会话工厂。

        参数：``session_factory`` 创建仓储事务所需的异步 SQLAlchemy 会话。
        返回：无。
        异常：不主动抛出业务异常。
        """
        self._session_factory = session_factory
        self._leases = lease_center or PostgresLeaseCenter(session_factory)

    async def get_by_turn(self, turn_id: str) -> MemoryEpisode | None:
        """按来源轮次读取回顾。

        参数：turn_id 是已完成 Run 的稳定 ID。
        返回：持久化回顾；不存在时返回 None。
        异常：数据库读取或严格 JSON 校验失败时向上传播。
        """
        async with self._session_factory() as session:
            row = await session.scalar(
                select(MemoryEpisodeModel).where(
                    MemoryEpisodeModel.turn_id == turn_id
                )
            )
        return None if row is None else _to_domain(row, project_id=row.project_id)

    async def create_if_absent(self, episode: MemoryEpisode, *, lease: LeaseGrant) -> MemoryEpisode | None:
        """按 turn_id 首写胜出保存回顾并核对冻结来源指纹。

        参数：episode 是已校验摘要、程序解析来源和输入指纹组成的记录。
        返回：已保存或并发胜出的回顾。
        异常：同一轮次出现不同来源指纹，或数据库校验失败时抛出。
        """
        if lease.key != LeaseKey("memory_processing", episode.turn_id):
            raise ValueError("memory_processing_lease_mismatch")
        values = {
            "episode_id": episode.episode_id,
            "turn_id": episode.turn_id,
            "session_id": episode.session_id,
            "project_id": episode.project_id,
            "content": episode.content,
            "summary_json": memory_domain_to_schema(episode.summary).model_dump(
                mode="json", exclude_none=True
            ),
            "source_refs_json": [
                memory_domain_to_schema(item).model_dump(mode="json", exclude_none=True)
                for item in episode.source_refs
            ],
            "source_fingerprint": episode.source_fingerprint,
            "created_at": episode.created_at or _now(),
        }
        async with self._session_factory() as session:
            async with session.begin():
                if await _project_source_is_suppressed(
                    session, project_id=episode.project_id, turn_id=episode.turn_id
                ):
                    return None
                await session.execute(
                    select(func.pg_advisory_xact_lock(_episode_suppression_lock_id(episode.turn_id)))
                )
                suppressed = await session.scalar(
                    select(MemorySourceSuppressionModel.source_id)
                    .where(
                        MemorySourceSuppressionModel.source_type == "episode_turn",
                        MemorySourceSuppressionModel.source_id == episode.turn_id,
                        MemorySourceSuppressionModel.item_fingerprint
                        == EPISODE_SUPPRESSION_FINGERPRINT,
                    )
                    .limit(1)
                )
                if suppressed is not None:
                    return None
                await self._leases.assert_current_in_transaction(session, lease)
                inserted_id = await session.scalar(
                    pg_insert(MemoryEpisodeModel)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=["turn_id"])
                    .returning(MemoryEpisodeModel.episode_id)
                )
                row = await session.scalar(
                    select(MemoryEpisodeModel).where(
                        MemoryEpisodeModel.turn_id == episode.turn_id
                    )
                )
                if row is None:
                    raise RuntimeError("memory_episode_not_saved")
                if row.source_fingerprint != episode.source_fingerprint:
                    raise ValueError("memory_episode_source_fingerprint_conflict")
                return _to_domain(row, project_id=row.project_id)

    async def list_recent(
        self, *, limit: int, offset: int, session_id: str | None = None
    ) -> tuple[list[MemoryEpisode], int]:
        """List visible episodes for memory management, excluding forgotten turns."""
        bounded_limit = min(100, max(1, limit))
        bounded_offset = max(0, offset)
        visible = ~select(MemorySourceSuppressionModel.source_id).where(
            MemorySourceSuppressionModel.source_type == "episode_turn",
            MemorySourceSuppressionModel.source_id == MemoryEpisodeModel.turn_id,
            MemorySourceSuppressionModel.item_fingerprint
            == EPISODE_SUPPRESSION_FINGERPRINT,
        ).exists()
        conditions = [SessionModel.deleted_time.is_(None), visible]
        if session_id:
            conditions.append(MemoryEpisodeModel.session_id == session_id)
        async with self._session_factory() as session:
            total = await session.scalar(
                select(func.count())
                .select_from(MemoryEpisodeModel)
                .join(SessionModel, SessionModel.id == MemoryEpisodeModel.session_id)
                .where(*conditions)
            ) or 0
            rows = (
                await session.execute(
                    select(
                        MemoryEpisodeModel,
                        MemoryEpisodeModel.project_id,
                    )
                    .join(SessionModel, SessionModel.id == MemoryEpisodeModel.session_id)
                    .where(*conditions)
                    .order_by(MemoryEpisodeModel.created_at.desc(), MemoryEpisodeModel.episode_id)
                    .offset(bounded_offset)
                    .limit(bounded_limit)
                )
            ).all()
        return (
            [_to_domain(row, project_id=project_id) for row, project_id in rows],
            int(total),
        )

    async def forget(self, episode_id: str) -> bool:
        """抑制来源轮次并删除任务回顾。"""
        async with self._session_factory() as session:
            async with session.begin():
                hint = await session.get(MemoryEpisodeModel, episode_id)
                if hint is None:
                    return False
                await session.execute(
                    select(func.pg_advisory_xact_lock(_episode_suppression_lock_id(hint.turn_id)))
                )
                episode = await session.scalar(
                    select(MemoryEpisodeModel)
                    .where(MemoryEpisodeModel.episode_id == episode_id)
                    .with_for_update()
                )
                if episode is None:
                    return False
                await session.execute(
                    pg_insert(MemorySourceSuppressionModel)
                    .values(
                        source_type="episode_turn",
                        source_id=episode.turn_id,
                        item_fingerprint=EPISODE_SUPPRESSION_FINGERPRINT,
                        created_at=_now(),
                    )
                    .on_conflict_do_nothing()
                )
                await session.execute(
                    sql_delete(MemoryEpisodeModel).where(
                        MemoryEpisodeModel.episode_id == episode_id
                    )
                )
        return True

    async def search(
        self, session_id: str, request: MemoryEpisodeSearchRequest
    ) -> list[MemoryEpisode]:
        """仅在指定会话内按任一词项搜索并限制候选数量。

        参数：session_id 是唯一可读范围；request 提供查询词项和召回上限。
        返回：按创建时间倒序排列的有限回顾列表。
        异常：数据库读取或严格 JSON 校验失败时向上传播。
        """
        literal_conditions = [
            MemoryEpisodeModel.content.ilike(
                f"%{_escape_like(term)}%", escape="\\"
            )
            for term in request.terms
        ]
        text_queries = [
            func.plainto_tsquery("chinese", term) for term in request.terms
        ]
        text_query = text_queries[0]
        for query in text_queries[1:]:
            text_query = text_query.op("||")(query)
        text_rank = func.ts_rank_cd(MemoryEpisodeModel.content_fts, text_query)
        scope_conditions = [
            MemoryEpisodeModel.session_id == session_id
        ]
        if request.project_id:
            scope_conditions.append(
                MemoryEpisodeModel.project_id == request.project_id
            )
        async with self._session_factory() as session:
            statement = (
                select(
                    MemoryEpisodeModel,
                    MemoryEpisodeModel.project_id,
                )
                .join(
                    SessionModel,
                    SessionModel.id == MemoryEpisodeModel.session_id,
                )
                .where(
                    SessionModel.deleted_time.is_(None),
                    ~select(MemorySourceSuppressionModel.source_id)
                    .where(
                        MemorySourceSuppressionModel.source_type == "episode_turn",
                        MemorySourceSuppressionModel.source_id
                        == MemoryEpisodeModel.turn_id,
                        MemorySourceSuppressionModel.item_fingerprint
                        == EPISODE_SUPPRESSION_FINGERPRINT,
                    )
                    .exists(),
                    or_(*scope_conditions),
                )
            )
            rows = (
                await session.execute(
                    statement.where(
                        MemoryEpisodeModel.content_fts.op("@@")(text_query)
                    )
                    .order_by(
                        text_rank.desc(), MemoryEpisodeModel.created_at.desc()
                    )
                    .limit(request.limit)
                )
            ).all()
            if not rows:
                rows = (
                    await session.execute(
                        statement.where(or_(*literal_conditions))
                        .order_by(MemoryEpisodeModel.created_at.desc())
                        .limit(request.limit)
                    )
                ).all()
        return [
            _to_domain(row, project_id=project_id)
            for row, project_id in rows
        ]

    async def expand_sources(
        self,
        episode_id: str,
        *,
        session_id: str,
        project_id: str | None,
        limit: int,
    ) -> tuple[MemorySourceReference, ...]:
        """核验回顾归属后，读取有限的对话、工具回执和结果原文。

        参数：episode_id 是已选回顾；session_id/project_id 是当前读取边界；
        limit 限制返回的来源数量，单条正文最多 1600 字符。
        返回：只包含仍属于当前会话或绑定项目的有效来源引用。
        异常：数据库错误、来源引用损坏或外置结果完整性校验失败时向上传播。
        """
        bounded_limit = min(4, max(0, limit))
        if bounded_limit == 0:
            return ()
        async with self._session_factory() as session:
            scope_conditions = [
                MemoryEpisodeModel.session_id == session_id
            ]
            if project_id is not None:
                scope_conditions.append(
                    MemoryEpisodeModel.project_id == project_id
                )
            episode_row = (
                await session.execute(
                    select(
                        MemoryEpisodeModel,
                        MemoryEpisodeModel.project_id,
                    )
                    .join(
                        SessionModel,
                        SessionModel.id == MemoryEpisodeModel.session_id,
                    )
                    .where(
                        MemoryEpisodeModel.episode_id == episode_id,
                        SessionModel.deleted_time.is_(None),
                        ~select(MemorySourceSuppressionModel.source_id)
                        .where(
                            MemorySourceSuppressionModel.source_type == "episode_turn",
                            MemorySourceSuppressionModel.source_id
                            == MemoryEpisodeModel.turn_id,
                            MemorySourceSuppressionModel.item_fingerprint
                            == EPISODE_SUPPRESSION_FINGERPRINT,
                        )
                        .exists(),
                        or_(*scope_conditions),
                    )
                )
            ).first()
            if episode_row is None:
                return ()
            episode, _ = episode_row
            references = tuple(
                memory_schema_to_domain(
                    MemorySourceReferenceSchema.model_validate(value)
                )
                for value in episode.source_refs_json
            )[:bounded_limit]
            expanded: list[MemorySourceReference] = []
            for reference in references:
                excerpt = await _load_source_excerpt(
                    session,
                    reference,
                    session_id=episode.session_id,
                    run_id=episode.turn_id,
                )
                if excerpt:
                    expanded.append(
                        replace(reference, excerpt=excerpt[:1600])
                    )
        return tuple(expanded)


def _to_domain(
    row: MemoryEpisodeModel, *, project_id: str
) -> MemoryEpisode:
    """严格恢复 ORM 行中的回顾摘要和来源。

    参数：``row`` 是已从 PostgreSQL 读取的 episode ORM 行。
    返回：领域层不可变任务回顾。
    异常：JSON 或 Pydantic 契约不合法时向上传播。
    """
    return MemoryEpisode(
        episode_id=row.episode_id,
        turn_id=row.turn_id,
        session_id=row.session_id,
        project_id=project_id,
        content=row.content,
        summary=memory_schema_to_domain(
            MemoryEpisodeSummarySchema.model_validate_json(
                json.dumps(row.summary_json, ensure_ascii=False)
            )
        ),
        source_refs=tuple(
            memory_schema_to_domain(
                MemorySourceReferenceSchema.model_validate(value)
            )
            for value in row.source_refs_json
        ),
        source_fingerprint=row.source_fingerprint,
        created_at=row.created_at,
    )


def _escape_like(value: str) -> str:
    """把用户词项按字面量用于 SQL LIKE 模式。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _load_source_excerpt(
    session: AsyncSession,
    reference: MemorySourceReference,
    *,
    session_id: str,
    run_id: str,
) -> str | None:
    """按类型和原始会话边界加载来源正文，并验证外置结果摘要。"""
    if reference.source_type == "turn":
        row = await session.scalar(
            select(MessageModel).where(
                MessageModel.id == reference.source_id,
                MessageModel.session_id == session_id,
                MessageModel.deleted_time.is_(None),
            )
        )
        return None if row is None else row.content
    if reference.source_type == "artifact":
        row = (
            await session.execute(
                select(
                    OrchestratorExecutionModel,
                    OrchestratorTaskModel.output_json,
                )
                .join(
                    OrchestratorTaskModel,
                    OrchestratorTaskModel.task_id
                    == OrchestratorExecutionModel.task_id,
                )
                .join(
                    OrchestratorPlanModel,
                    OrchestratorPlanModel.plan_id
                    == OrchestratorTaskModel.plan_id,
                )
                .join(
                    AgentRunModel,
                    AgentRunModel.run_id == OrchestratorPlanModel.run_id,
                )
                .where(
                    OrchestratorExecutionModel.execution_id == reference.source_id,
                    OrchestratorExecutionModel.status == "completed",
                    OrchestratorExecutionModel.worker_result_id.is_not(None),
                    OrchestratorTaskModel.status == "completed",
                    OrchestratorPlanModel.run_id == run_id,
                    AgentRunModel.session_id == session_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        execution, task_output = row
        if not isinstance(execution.result_json, dict) or not isinstance(
            task_output, dict
        ):
            return None
        if task_output != execution.result_json:
            raise RuntimeError("memory_episode_source_integrity_failure")
        serialized = json.dumps(
            execution.result_json,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return serialized.decode("utf-8")
    if reference.source_type == "run":
        source_run = await session.scalar(
            select(AgentRunModel.run_id).where(
                AgentRunModel.run_id == reference.source_id,
                AgentRunModel.session_id == session_id,
            )
        )
        return reference.excerpt if source_run is not None else None
    return None


def _bounded_json(value: Any) -> str:
    """序列化工具证据，控制异常大结果进入模型上下文。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


__all__ = ["PostgresMemoryEpisodeRepository"]
