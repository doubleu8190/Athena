"""项目 ORM 行到领域模型转换。"""

from __future__ import annotations

from datetime import UTC, datetime

from adapter.project.model.project import ProjectModel
from domain.project import Project


def _as_utc(value: datetime) -> datetime:
    """补齐 SQLite 等测试驱动丢失的 UTC 时区信息。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def project_to_domain(row: ProjectModel) -> Project:
    """把项目 ORM 行转换为领域对象。"""
    return Project(
        id=row.id,
        owner_id=row.owner_id,
        name=row.name,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
        deleted_time=_as_utc(row.deleted_time) if row.deleted_time is not None else None,
    )


__all__ = ["project_to_domain"]
