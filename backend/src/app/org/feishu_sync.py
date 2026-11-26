"""飞书通讯录组织树校验（环路检测、拓扑排序）与原子同步编排。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from common.date import as_utc
from common.ids import new_id, now_utc
from domain.auth import AuthContext
from domain.org import (
    DepartmentCycleError,
    FeishuDirectoryClient,
    FeishuSyncSummary,
    OrgRepository,
    OrgSyncValidationError,
    RemoteDepartment,
    RemoteEmployee,
)

MAX_DEPARTMENT_DEPTH = 64


def validate_and_order_departments(
    departments: Sequence[RemoteDepartment],
) -> list[RemoteDepartment]:
    """在内存中执行部门树去重、环路检测与深度校验，并按父先于子拓扑排序。

    发现在职部门中存在任何循环引用（自环、双向环或多节点环）时，立即抛出
    DepartmentCycleError，避免部分写入破坏数据库内既有部门树。
    """
    active_by_ext: dict[str, RemoteDepartment] = {}
    for dept in departments:
        if dept.is_deleted:
            continue
        ext_id = dept.external_id.strip()
        if not ext_id:
            raise OrgSyncValidationError("department_external_id_empty")
        if ext_id in active_by_ext:
            raise OrgSyncValidationError("duplicate_department_external_id")
        parent_ext = dept.parent_external_id.strip() if dept.parent_external_id else None
        if parent_ext == "0":
            parent_ext = None
        active_by_ext[ext_id] = RemoteDepartment(
            external_id=ext_id,
            parent_external_id=parent_ext,
            name=(dept.name.strip() or ext_id)[:120],
            order_index=dept.order_index,
            is_deleted=False,
        )

    # 使用三色/深度记忆化沿父指针向上追溯，精确识别自环与任意长度环路。
    depths: dict[str, int] = {}
    for ext_id in active_by_ext:
        if ext_id in depths:
            continue
        chain: list[str] = []
        visiting: set[str] = set()
        current: str | None = ext_id
        while current is not None and current in active_by_ext and current not in depths:
            if current in visiting:
                raise DepartmentCycleError("department_cycle_detected")
            visiting.add(current)
            chain.append(current)
            parent = active_by_ext[current].parent_external_id
            # 可见范围外的上游父部门视作根边界，不视为错误。
            current = parent if (parent is not None and parent in active_by_ext) else None

        base_depth = depths[current] if current is not None and current in depths else 0
        for offset, node_id in enumerate(reversed(chain), start=1):
            node_depth = base_depth + offset
            if node_depth > MAX_DEPARTMENT_DEPTH:
                raise OrgSyncValidationError("department_depth_exceeded")
            depths[node_id] = node_depth

    ordered_ids = sorted(
        active_by_ext.keys(),
        key=lambda ext_id: (
            depths[ext_id],
            active_by_ext[ext_id].order_index,
            ext_id,
        ),
    )
    return [active_by_ext[ext_id] for ext_id in ordered_ids]


def _combine_employees(base: RemoteEmployee, incoming: RemoteEmployee) -> RemoteEmployee:
    """无损合并同一员工的多条记录，保留顺序去重后的全部部门归属与首个非空资料。"""
    combined_depts = list(base.department_external_ids)
    for dept_ext_id in incoming.department_external_ids:
        if dept_ext_id not in combined_depts:
            combined_depts.append(dept_ext_id)
    return RemoteEmployee(
        union_id=base.union_id or incoming.union_id,
        open_id=base.open_id or incoming.open_id,
        name=base.name or incoming.name,
        email=base.email or incoming.email,
        employee_no=base.employee_no or incoming.employee_no,
        avatar_url=base.avatar_url or incoming.avatar_url,
        job_title=base.job_title or incoming.job_title,
        department_external_ids=tuple(combined_depts),
        is_active=base.is_active and incoming.is_active,
    )


def _merge_remote_employees(
    employees: Sequence[RemoteEmployee],
) -> tuple[list[RemoteEmployee], list[RemoteEmployee]]:
    """按 union_id/open_id 合并多部门员工记录并拆分为在职与离职/停用两组。"""
    merged: dict[str, RemoteEmployee] = {}
    open_to_key: dict[str, str] = {}

    for emp in employees:
        if emp.union_id is None and emp.open_id is None:
            continue
        key = (
            f"u:{emp.union_id}"
            if emp.union_id is not None
            else open_to_key.get(emp.open_id or "", f"o:{emp.open_id}")
        )
        # 若先前分别存在 u:{union_id} 与 o:{open_id} 两条记录，合并后再汇合当前记录，防覆盖丢失。
        if emp.union_id is not None and emp.open_id is not None:
            prior_open_key = f"o:{emp.open_id}"
            if prior_open_key in merged and key != prior_open_key:
                prior = merged.pop(prior_open_key)
                existing_by_union = merged.get(key)
                merged[key] = (
                    prior
                    if existing_by_union is None
                    else _combine_employees(existing_by_union, prior)
                )
            open_to_key[emp.open_id] = key

        existing = merged.get(key)
        merged[key] = emp if existing is None else _combine_employees(existing, emp)

    active: list[RemoteEmployee] = []
    inactive: list[RemoteEmployee] = []
    for emp in merged.values():
        if emp.is_active:
            active.append(emp)
        else:
            inactive.append(emp)
    return active, inactive


class FeishuOrgSyncService:
    """编排飞书部门树与员工花名册拉取、内存环路校验及单事务原子落库。"""

    def __init__(
        self,
        feishu_client: FeishuDirectoryClient,
        org_repository: OrgRepository,
        *,
        clock: Callable[[], datetime] = now_utc,
        department_id_factory: Callable[[], str] | None = None,
        user_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._feishu_client = feishu_client
        self._org_repository = org_repository
        self._clock = clock
        self._department_id_factory = department_id_factory or (lambda: new_id("dept"))
        self._user_id_factory = user_id_factory or (lambda: new_id("usr"))

    async def sync(self, *, actor: AuthContext | None = None) -> FeishuSyncSummary:
        """拉取飞书组织树与员工，校验无环后在同一数据库事务内原子同步并吊销离职会话。"""
        remote_departments = await self._feishu_client.list_departments()
        # 必须在任何数据库写入前于内存中完成环路检测与拓扑排序。
        ordered_departments = validate_and_order_departments(remote_departments)

        department_ids_to_query = ["0"]
        for dept in ordered_departments:
            if dept.external_id not in department_ids_to_query:
                department_ids_to_query.append(dept.external_id)

        collected_employees: list[RemoteEmployee] = []
        for dept_ext_id in department_ids_to_query:
            dept_users = await self._feishu_client.list_department_users(dept_ext_id)
            collected_employees.extend(dept_users)

        active_employees, inactive_employees = _merge_remote_employees(collected_employees)
        now = as_utc(self._clock())
        return await self._org_repository.apply_feishu_sync(
            ordered_departments=ordered_departments,
            active_employees=active_employees,
            inactive_employees=inactive_employees,
            now=now,
            department_id_factory=self._department_id_factory,
            user_id_factory=self._user_id_factory,
            actor=actor,
        )


__all__ = [
    "FeishuOrgSyncService",
    "MAX_DEPARTMENT_DEPTH",
    "validate_and_order_departments",
]
