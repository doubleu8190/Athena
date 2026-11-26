"""组织架构 ORM 行到领域模型转换。"""

from __future__ import annotations

from adapter.org.model import DepartmentModel
from common.date import as_utc
from domain.org import Department, DepartmentStatus


def department_to_domain(row: DepartmentModel) -> Department:
    """把部门 ORM 行转换为领域 Department。"""
    return Department(
        id=row.id,
        external_id=row.external_id,
        parent_id=row.parent_id,
        name=row.name,
        path=row.path,
        order_index=row.order_index,
        status=DepartmentStatus(row.status),
        synced_at=as_utc(row.synced_at) if row.synced_at is not None else None,
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
    )


__all__ = ["department_to_domain"]
