"""会话 ORM 行到领域对象转换。"""

from __future__ import annotations

from adapter.session.model.session import SessionModel
from common.date import as_utc
from domain.session import Session


def session_to_domain(row: SessionModel) -> Session:
    """把会话 ORM 行转换为最小领域对象。"""
    return Session(
        id=row.id,
        owner_id=row.owner_id,
        title=row.title,
        project_id=row.project_id,
        last_message_sequence=row.last_message_sequence,
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        deleted_time=as_utc(row.deleted_time) if row.deleted_time is not None else None,
    )


__all__ = ["session_to_domain"]
