"""系统提示词 ORM 行到领域版本的转换。"""

from __future__ import annotations

from adapter.system_prompt.model import SystemPromptVersionModel
from common.date import as_utc
from domain.system_prompt import SystemPromptKind, SystemPromptVersion


def system_prompt_version_to_domain(row: SystemPromptVersionModel) -> SystemPromptVersion:
    """把数据库行投影为带时区时间的不可变提示词版本。"""
    created_at = as_utc(row.created_at)
    return SystemPromptVersion(
        id=row.id,
        kind=SystemPromptKind(row.kind),
        content=row.content,
        created_by=row.created_by,
        created_at=created_at,
        restored_from_version_id=row.restored_from_version_id,
    )


__all__ = ["system_prompt_version_to_domain"]
