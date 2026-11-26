"""Subagent ORM 模型与领域实体转换器。"""

from __future__ import annotations

from adapter.subagent.model import SubagentModel
from common.date import as_utc
from domain.access import VisibilityGrant, VisibilityScope
from domain.subagent import SubagentProfile


def subagent_to_domain(
    row: SubagentModel,
    *,
    grants: tuple[VisibilityGrant, ...] = (),
) -> SubagentProfile:
    """将 Subagent ORM 行转换为不可变领域实体。"""
    return SubagentProfile(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        system_prompt=row.system_prompt,
        model_profile_id=row.model_profile_id,
        allowed_tool_ids=tuple(str(item) for item in (row.allowed_tool_ids or ())),
        enabled=bool(row.enabled),
        max_llm_requests=row.max_llm_requests,
        timeout_seconds=row.timeout_seconds,
        version=int(row.version),
        created_by=row.created_by,
        deleted_at=None if row.deleted_at is None else as_utc(row.deleted_at),
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        visibility_scope=VisibilityScope(row.visibility_scope),
        owner_department_id=row.owner_department_id,
        visibility_grants=grants,
    )


__all__ = ["subagent_to_domain"]
