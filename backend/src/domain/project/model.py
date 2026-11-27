"""项目管理领域实体、分页对象与业务异常。"""

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
    description: str | None = None
    instructions: str | None = None
    default_skill_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectPage:
    """按最近修改时间排序的有效项目分页。"""

    items: tuple[Project, ...]
    total: int


class ProjectNotFoundError(LookupError):
    """项目不存在或已经软删除。"""


class InvalidProjectNameError(ValueError):
    """项目名称不满足展示名称约束。"""


class InvalidProjectInputError(ValueError):
    """项目简介、背景指令、默认技能或局部更新参数不满足业务约束。"""


class ProjectHasSessionsError(ValueError):
    """项目仍有关联的未删除会话。"""


__all__ = [
    "InvalidProjectInputError",
    "InvalidProjectNameError",
    "Project",
    "ProjectHasSessionsError",
    "ProjectNotFoundError",
    "ProjectPage",
]
