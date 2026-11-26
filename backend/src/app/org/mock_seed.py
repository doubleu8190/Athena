"""非生产本地 Mock 组织树与多角色测试账号幂等种子数据。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from common.date import as_utc
from common.ids import now_utc
from domain.auth import AuthProvider, User, UserRole, UserStatus
from domain.org import (
    MockDepartmentSeed,
    MockDeptAdminScopeSeed,
    MockUserDepartmentSeed,
    OrgRepository,
)

MOCK_DEPT_ROOT_ID = "dept_mock_root"
MOCK_DEPT_RD_ID = "dept_mock_rd"
MOCK_DEPT_PLATFORM_ID = "dept_mock_platform"
MOCK_DEPT_FIN_ID = "dept_mock_fin"
MOCK_DEPT_HR_ID = "dept_mock_hr"

MOCK_DEPARTMENTS: tuple[MockDepartmentSeed, ...] = (
    MockDepartmentSeed(
        id=MOCK_DEPT_ROOT_ID,
        external_id="mock_dept_root",
        parent_id=None,
        name="集团总部",
        path=f"/{MOCK_DEPT_ROOT_ID}/",
        order_index=1,
    ),
    MockDepartmentSeed(
        id=MOCK_DEPT_RD_ID,
        external_id="mock_dept_rd",
        parent_id=MOCK_DEPT_ROOT_ID,
        name="研发部",
        path=f"/{MOCK_DEPT_ROOT_ID}/{MOCK_DEPT_RD_ID}/",
        order_index=10,
    ),
    MockDepartmentSeed(
        id=MOCK_DEPT_PLATFORM_ID,
        external_id="mock_dept_platform",
        parent_id=MOCK_DEPT_RD_ID,
        name="平台架构组",
        path=f"/{MOCK_DEPT_ROOT_ID}/{MOCK_DEPT_RD_ID}/{MOCK_DEPT_PLATFORM_ID}/",
        order_index=11,
    ),
    MockDepartmentSeed(
        id=MOCK_DEPT_FIN_ID,
        external_id="mock_dept_fin",
        parent_id=MOCK_DEPT_ROOT_ID,
        name="财务部",
        path=f"/{MOCK_DEPT_ROOT_ID}/{MOCK_DEPT_FIN_ID}/",
        order_index=20,
    ),
    MockDepartmentSeed(
        id=MOCK_DEPT_HR_ID,
        external_id="mock_dept_hr",
        parent_id=MOCK_DEPT_ROOT_ID,
        name="人力资源部",
        path=f"/{MOCK_DEPT_ROOT_ID}/{MOCK_DEPT_HR_ID}/",
        order_index=30,
    ),
)

MOCK_USER_SPECS: tuple[tuple[str, str, str, UserRole, str, str, str], ...] = (
    (
        "mock_super_admin",
        "系统超管",
        "mock_super_admin@mock.athena.local",
        UserRole.SUPER_ADMIN,
        "MOCK001",
        "系统超级管理员",
        MOCK_DEPT_ROOT_ID,
    ),
    (
        "mock_fin_admin",
        "财务主管",
        "mock_fin_admin@mock.athena.local",
        UserRole.DEPT_ADMIN,
        "MOCK002",
        "财务部负责人",
        MOCK_DEPT_FIN_ID,
    ),
    (
        "mock_rd_admin",
        "研发主管",
        "mock_rd_admin@mock.athena.local",
        UserRole.DEPT_ADMIN,
        "MOCK003",
        "研发部负责人",
        MOCK_DEPT_RD_ID,
    ),
    (
        "mock_fin_member",
        "财务员工",
        "mock_fin_member@mock.athena.local",
        UserRole.MEMBER,
        "MOCK004",
        "高级财务专员",
        MOCK_DEPT_FIN_ID,
    ),
    (
        "mock_rd_member",
        "研发员工",
        "mock_rd_member@mock.athena.local",
        UserRole.MEMBER,
        "MOCK005",
        "平台架构师",
        MOCK_DEPT_PLATFORM_ID,
    ),
    (
        "mock_hr_member",
        "人事员工",
        "mock_hr_member@mock.athena.local",
        UserRole.MEMBER,
        "MOCK006",
        "人力资源专员",
        MOCK_DEPT_HR_ID,
    ),
)

MOCK_ADMIN_SCOPES: tuple[MockDeptAdminScopeSeed, ...] = (
    MockDeptAdminScopeSeed(
        user_id="mock_fin_admin",
        department_id=MOCK_DEPT_FIN_ID,
        include_subdepartments=True,
    ),
    MockDeptAdminScopeSeed(
        user_id="mock_rd_admin",
        department_id=MOCK_DEPT_RD_ID,
        include_subdepartments=True,
    ),
)


def build_mock_seed_data(
    now: datetime,
) -> tuple[
    tuple[MockDepartmentSeed, ...],
    tuple[User, ...],
    tuple[MockUserDepartmentSeed, ...],
    tuple[MockDeptAdminScopeSeed, ...],
]:
    """构造非生产 Mock 组织树、6 个标准角色测试账号及其部门归属与分权范围。"""
    current = as_utc(now)
    users: list[User] = []
    bindings: list[MockUserDepartmentSeed] = []
    for user_id, display_name, email, role, employee_no, job_title, dept_id in MOCK_USER_SPECS:
        users.append(
            User(
                id=user_id,
                email=email,
                password_hash=None,
                display_name=display_name,
                status=UserStatus.ACTIVE,
                role=role,
                auth_provider=AuthProvider.MOCK,
                employee_no=employee_no,
                job_title=job_title,
                last_synced_at=current,
                created_at=current,
                updated_at=current,
                deleted_time=None,
            )
        )
        bindings.append(
            MockUserDepartmentSeed(
                user_id=user_id,
                department_id=dept_id,
                is_primary=True,
            )
        )
    return MOCK_DEPARTMENTS, tuple(users), tuple(bindings), MOCK_ADMIN_SCOPES


async def seed_mock_organization(
    org_repository: OrgRepository,
    *,
    clock: Callable[[], datetime] = now_utc,
) -> None:
    """幂等写入非生产本地 Mock 组织树与 6 个典型角色测试账号。"""
    now = clock()
    departments, users, bindings, scopes = build_mock_seed_data(now)
    await org_repository.seed_mock_organization(
        departments=departments,
        users=users,
        user_departments=bindings,
        admin_scopes=scopes,
        now=now,
    )


__all__ = [
    "MOCK_ADMIN_SCOPES",
    "MOCK_DEPARTMENTS",
    "MOCK_DEPT_FIN_ID",
    "MOCK_DEPT_HR_ID",
    "MOCK_DEPT_PLATFORM_ID",
    "MOCK_DEPT_RD_ID",
    "MOCK_DEPT_ROOT_ID",
    "MOCK_USER_SPECS",
    "build_mock_seed_data",
    "seed_mock_organization",
]
