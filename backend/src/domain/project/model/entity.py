"""项目管理领域实体与分页对象。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Project:
    """项目目录中的一个有效或软删除项目。"""

    id: str
    owner_id: str
    name: str
    created_at: datetime
    updated_at: datetime
    deleted_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProjectPage:
    """按最近修改时间排序的有效项目分页。"""

    items: tuple[Project, ...]
    total: int


__all__ = ["Project", "ProjectPage"]
