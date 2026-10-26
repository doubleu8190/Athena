"""会话标题生成任务的 PostgreSQL 租约仓储。"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.sessions import SessionTitleJob, SessionTitleJobRepository

from ..models import SessionModel, SessionTitleJobModel


class PostgresSessionTitleJobRepository(SessionTitleJobRepository):
    """使用行锁领取任务，并用 attempt 防止过期领取者提交。"""

    def __init__(
        self,
        session_factory: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ],
    ) -> None:
        self._session_factory = session_factory

    async def claim_next(
        self,
        *,
        now: datetime,
        worker_id: str,
        lease_seconds: int,
    ) -> SessionTitleJob | None:
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(SessionTitleJobModel)
                    .where(
                        or_(
                            (SessionTitleJobModel.status == "queued")
                            & (SessionTitleJobModel.available_at <= now_text),
                            (SessionTitleJobModel.status == "running")
                            & (SessionTitleJobModel.lease_expires_at <= now_text),
                        )
                    )
                    .order_by(SessionTitleJobModel.available_at, SessionTitleJobModel.session_id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if row is None:
                    return None
                row.status = "running"
                row.attempt += 1
                row.claimed_by = worker_id
                row.lease_expires_at = lease_until
                row.updated_at = now_text
                await session.flush()
                try:
                    attachment_names = json.loads(row.attachment_names_json)
                except (TypeError, ValueError):
                    attachment_names = []
                return SessionTitleJob(
                    session_id=row.session_id,
                    source_message_id=row.source_message_id,
                    expected_title_revision=row.expected_title_revision,
                    attempt=row.attempt,
                    attachment_names=tuple(
                        item for item in attachment_names if isinstance(item, str)
                    ),
                )

    async def complete(
        self, job: SessionTitleJob, *, title: str, now: datetime
    ) -> bool:
        now_text = now.isoformat()
        async with self._session_factory() as session:
            async with session.begin():
                session_row = await session.scalar(
                    select(SessionModel)
                    .where(SessionModel.id == job.session_id)
                    .with_for_update()
                )
                row = await session.scalar(
                    select(SessionTitleJobModel)
                    .where(SessionTitleJobModel.session_id == job.session_id)
                    .with_for_update()
                )
                if row is None or row.status != "running" or row.attempt != job.attempt:
                    return False
                if (
                    session_row is None
                    or session_row.deleted_time is not None
                    or session_row.title_source != "fallback"
                    or session_row.title_revision != job.expected_title_revision
                ):
                    row.status = "cancelled"
                    row.lease_expires_at = None
                    row.updated_at = now_text
                    return False
                session_row.title = title
                session_row.title_source = "generated"
                session_row.title_revision += 1
                session_row.updated_at = now_text
                row.status = "completed"
                row.lease_expires_at = None
                row.claimed_by = None
                row.error_code = None
                row.updated_at = now_text
        return True

    async def fail(
        self,
        job: SessionTitleJob,
        *,
        error_code: str,
        retry_at: datetime | None,
        now: datetime,
    ) -> None:
        now_text = now.isoformat()
        async with self._session_factory() as session:
            async with session.begin():
                session_row = await session.scalar(
                    select(SessionModel)
                    .where(SessionModel.id == job.session_id)
                    .with_for_update()
                )
                row = await session.scalar(
                    select(SessionTitleJobModel)
                    .where(SessionTitleJobModel.session_id == job.session_id)
                    .with_for_update()
                )
                if row is None or row.status != "running" or row.attempt != job.attempt:
                    return
                row.lease_expires_at = None
                row.claimed_by = None
                row.error_code = error_code
                row.updated_at = now_text
                if (
                    session_row is None
                    or session_row.deleted_time is not None
                    or session_row.title_source != "fallback"
                    or session_row.title_revision != job.expected_title_revision
                ):
                    row.status = "cancelled"
                elif retry_at is None:
                    row.status = "failed"
                else:
                    row.status = "queued"
                    row.available_at = retry_at.isoformat()


__all__ = ["PostgresSessionTitleJobRepository"]
