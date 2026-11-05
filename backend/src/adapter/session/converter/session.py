"""会话 ORM 行到领域对象转换。"""

from __future__ import annotations

from datetime import UTC, datetime

from adapter.session.model.session import SessionModel
from domain.session import Session


def _as_utc(value: datetime) -> datetime:
    """补齐 SQLite 等测试驱动丢失的 UTC 时区信息。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def session_to_domain(row: SessionModel) -> Session:
    """把会话 ORM 行转换为最小领域对象。"""
    return Session(
        id=row.id,
        owner_id=row.owner_id,
        title=row.title,
        project_id=row.project_id,
        last_message_sequence=row.last_message_sequence,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
        deleted_time=_as_utc(row.deleted_time) if row.deleted_time is not None else None,
    )


__all__ = ["session_to_domain"]
