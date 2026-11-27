"""项目 ORM 行到领域模型转换。"""

from __future__ import annotations

from adapter.project.model import ProjectModel
from common.date import as_utc
from domain.project import Project


def project_to_domain(row: ProjectModel) -> Project:
    """把项目 ORM 行转换为领域对象。"""
    return Project(
        id=row.id,
        owner_id=row.owner_id,
        name=row.name,
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        deleted_time=as_utc(row.deleted_time) if row.deleted_time is not None else None,
        description=row.description,
        instructions=row.instructions,
        default_skill_ids=tuple(row.default_skill_ids or ()),
    )


__all__ = ["project_to_domain"]
