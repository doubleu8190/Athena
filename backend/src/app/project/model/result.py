"""供 HTTP 层使用的项目应用结果模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ProjectResult:
    """不暴露软删除状态的稳定应用结果。"""

    id: str
    name: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectPageResult:
    """项目应用分页结果。"""

    items: tuple[ProjectResult, ...]
    total: int


__all__ = ["ProjectPageResult", "ProjectResult"]
