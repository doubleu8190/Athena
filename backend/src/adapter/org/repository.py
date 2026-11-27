"""企业组织树、分权范围与飞书通讯录同步的 async SQLAlchemy 仓储。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.auth.converter import user_to_domain
from adapter.auth.model import AuthSessionModel, UserModel
from adapter.auth.repository.transaction_guard import verify_active_session_user
from adapter.org.model import DepartmentModel, DeptAdminScopeModel, UserDepartmentModel
from common.date import as_utc
from domain.auth import AuthContext, AuthProvider, User, UserRole, UserStatus
from domain.org import (
    Department,
    DepartmentNotFoundError,
    DepartmentStatus,
    DeptAdminScopeDetail,
    DeptAdminScopeInput,
    FeishuSyncSummary,
    InvalidDeptAdminScopeError,
    LastSuperAdminProtectedError,
    MockDepartmentSeed,
    MockDeptAdminScopeSeed,
    MockUserDepartmentSeed,
    OrgAuthorizationError,
    OrgSyncValidationError,
    OrgUserDetail,
    OrgUserNotFoundError,
    OrgUserPage,
    RemoteDepartment,
    RemoteEmployee,
    UserDepartmentSummary,
    UserOrgMembership,
)


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


MAX_DEPARTMENT_PATH_LENGTH = 2048
MAX_EMAIL_LENGTH = 320
_MOCK_DEPARTMENT_EXTERNAL_PREFIX = "mock_dept_"


def _escape_like(term: str) -> str:
    """转义 SQL LIKE 通配符，防止用户输入 %/_ 改变模糊检索语义。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_optional_email(email: str | None) -> str | None:
    """规范化飞书同步邮箱；为空或格式非法时降级返回 None，避免阻断全量同步。"""
    if email is None:
        return None
    candidate = email.strip()
    if not candidate or len(candidate) > MAX_EMAIL_LENGTH:
        return None
    try:
        validated = validate_email(candidate, check_deliverability=False)
    except EmailNotValidError:
        return None
    normalized = validated.normalized.lower()
    return normalized if len(normalized) <= MAX_EMAIL_LENGTH else None


class SqlAlchemyOrgRepository:
    """基于 async SQLAlchemy 维护部门树、成员归属、分权范围及通讯录原子同步。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """保存异步会话工厂。"""
        self._session_factory = session_factory

    async def get_user_org_membership(self, user_id: str) -> UserOrgMembership:
        """读取指定用户的活跃部门列表及已授权管辖部门 ID 列表。"""
        async with self._session_factory() as session:
            dept_map, scope_map = await self._load_org_relations(session, [user_id])
        departments = tuple(dept_map.get(user_id, ()))
        scopes = scope_map.get(user_id, ())
        return UserOrgMembership(
            departments=departments,
            managed_department_ids=tuple(scope.department_id for scope in scopes),
        )

    async def list_departments(
        self,
        *,
        include_archived: bool = False,
    ) -> tuple[Department, ...]:
        """按物化路径与同级排序索引返回部门列表。"""
        async with self._session_factory() as session:
            stmt = select(DepartmentModel)
            if not include_archived:
                stmt = stmt.where(DepartmentModel.status == DepartmentStatus.ACTIVE.value)
            stmt = stmt.order_by(
                DepartmentModel.path.asc(),
                DepartmentModel.order_index.asc(),
                DepartmentModel.id.asc(),
            )
            rows = (await session.scalars(stmt)).all()
        return tuple(department_to_domain(row) for row in rows)

    async def list_users(
        self,
        *,
        department_id: str | None = None,
        include_subdepartments: bool = True,
        role: UserRole | None = None,
        status: UserStatus | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> OrgUserPage:
        """按部门子树（物化路径前缀匹配）、角色、状态与关键词分页查询未删除用户。"""
        async with self._session_factory() as session:
            stmt = select(UserModel).where(UserModel.deleted_time.is_(None))
            if department_id is not None:
                target_dept = await session.get(DepartmentModel, department_id)
                if target_dept is None:
                    return OrgUserPage(items=(), total=0, page=page, page_size=page_size)
                if include_subdepartments:
                    # 借助规范物化路径 /{root}/.../{dept_id}/ 前缀匹配子树，避免跨分支误匹配；
                    # 目标部门为 active 时同时排除已归档子部门，防止误统计历史归档节点成员。
                    sub_conditions = [
                        UserDepartmentModel.user_id == UserModel.id,
                        DepartmentModel.path.startswith(target_dept.path, autoescape=True),
                    ]
                    if target_dept.status == DepartmentStatus.ACTIVE.value:
                        sub_conditions.append(
                            DepartmentModel.status == DepartmentStatus.ACTIVE.value
                        )
                    dept_match = exists(
                        select(1)
                        .select_from(UserDepartmentModel)
                        .join(
                            DepartmentModel,
                            DepartmentModel.id == UserDepartmentModel.department_id,
                        )
                        .where(*sub_conditions)
                    )
                else:
                    dept_match = exists(
                        select(1).where(
                            UserDepartmentModel.user_id == UserModel.id,
                            UserDepartmentModel.department_id == target_dept.id,
                        )
                    )
                stmt = stmt.where(dept_match)
            if role is not None:
                stmt = stmt.where(UserModel.role == role.value)
            if status is not None:
                stmt = stmt.where(UserModel.status == status.value)
            if query is not None and query.strip():
                pattern = f"%{_escape_like(query.strip())}%"
                stmt = stmt.where(
                    or_(
                        UserModel.display_name.ilike(pattern, escape="\\"),
                        UserModel.email.ilike(pattern, escape="\\"),
                        UserModel.employee_no.ilike(pattern, escape="\\"),
                    )
                )

            total = int(
                (await session.scalar(select(func.count()).select_from(stmt.subquery()))) or 0
            )
            page_rows = (
                await session.scalars(
                    stmt.order_by(UserModel.created_at.asc(), UserModel.id.asc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
            user_ids = [row.id for row in page_rows]
            dept_map, scope_map = await self._load_org_relations(session, user_ids)

        items = tuple(self._build_user_detail(row, dept_map, scope_map) for row in page_rows)
        return OrgUserPage(items=items, total=total, page=page, page_size=page_size)

    async def update_user_role(
        self,
        actor: AuthContext,
        user_id: str,
        *,
        role: UserRole,
        managed_departments: tuple[DeptAdminScopeInput, ...],
        now: datetime,
    ) -> OrgUserDetail:
        """在单事务内复核超管、保护最后一位活跃超管，并原子更新角色与部门管辖范围。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_super_admin_in_tx(session, actor, now)

                effective_scopes = self._validate_scope_inputs(role, managed_departments)
                if effective_scopes:
                    requested_dept_ids = [item.department_id for item in effective_scopes]
                    active_dept_ids = set(
                        (
                            await session.scalars(
                                select(DepartmentModel.id).where(
                                    DepartmentModel.id.in_(requested_dept_ids),
                                    DepartmentModel.status == DepartmentStatus.ACTIVE.value,
                                )
                            )
                        ).all()
                    )
                    if len(active_dept_ids) != len(requested_dept_ids):
                        raise DepartmentNotFoundError("department_not_found")

                target = await session.scalar(
                    select(UserModel)
                    .where(
                        UserModel.id == user_id,
                        UserModel.deleted_time.is_(None),
                    )
                    .with_for_update()
                )
                if target is None:
                    raise OrgUserNotFoundError("user_not_found")

                # 防止并发或单次请求将系统内最后一位活跃超级管理员降权，导致管理后台锁死。
                if (
                    target.role == UserRole.SUPER_ADMIN.value
                    and target.status == UserStatus.ACTIVE.value
                    and role is not UserRole.SUPER_ADMIN
                ):
                    await self._ensure_another_active_super_admin(session, target.id)

                target.role = role.value
                target.updated_at = now
                await session.execute(
                    delete(DeptAdminScopeModel).where(DeptAdminScopeModel.user_id == target.id)
                )
                for scope in effective_scopes:
                    session.add(
                        DeptAdminScopeModel(
                            user_id=target.id,
                            department_id=scope.department_id,
                            include_subdepartments=scope.include_subdepartments,
                            granted_by=actor.user.id,
                            created_at=now,
                        )
                    )
                await session.flush()
                dept_map, scope_map = await self._load_org_relations(session, [target.id])
                return self._build_user_detail(target, dept_map, scope_map)

    async def update_user_status(
        self,
        actor: AuthContext,
        user_id: str,
        *,
        status: UserStatus,
        now: datetime,
    ) -> OrgUserDetail:
        """在单事务内复核超管并切换用户启停状态；停用时同事务吊销该用户全部活跃会话。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_super_admin_in_tx(session, actor, now)

                target = await session.scalar(
                    select(UserModel)
                    .where(
                        UserModel.id == user_id,
                        UserModel.deleted_time.is_(None),
                    )
                    .with_for_update()
                )
                if target is None:
                    raise OrgUserNotFoundError("user_not_found")

                # 禁止停用系统内最后一个活跃超级管理员。
                if (
                    status is UserStatus.DISABLED
                    and target.role == UserRole.SUPER_ADMIN.value
                    and target.status == UserStatus.ACTIVE.value
                ):
                    await self._ensure_another_active_super_admin(session, target.id)

                target.status = status.value
                target.updated_at = now
                if status is UserStatus.DISABLED:
                    # 停用账号必须同事务批量吊销全部未撤销会话，确保既有 Cookie 立刻失效。
                    await session.execute(
                        update(AuthSessionModel)
                        .where(
                            AuthSessionModel.user_id == target.id,
                            AuthSessionModel.revoked_time.is_(None),
                        )
                        .values(revoked_time=now)
                    )
                await session.flush()
                dept_map, scope_map = await self._load_org_relations(session, [target.id])
                return self._build_user_detail(target, dept_map, scope_map)

    async def list_active_mock_users(self) -> tuple[OrgUserDetail, ...]:
        """读取全部活跃的非生产 Mock 账号及其部门、管辖范围。"""
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(UserModel)
                    .where(
                        UserModel.auth_provider == AuthProvider.MOCK.value,
                        UserModel.status == UserStatus.ACTIVE.value,
                        UserModel.deleted_time.is_(None),
                    )
                    .order_by(UserModel.employee_no.asc(), UserModel.id.asc())
                )
            ).all()
            user_ids = [row.id for row in rows]
            dept_map, scope_map = await self._load_org_relations(session, user_ids)
        return tuple(self._build_user_detail(row, dept_map, scope_map) for row in rows)

    async def seed_mock_organization(
        self,
        *,
        departments: Sequence[MockDepartmentSeed],
        users: Sequence[User],
        user_departments: Sequence[MockUserDepartmentSeed],
        admin_scopes: Sequence[MockDeptAdminScopeSeed],
        now: datetime,
    ) -> None:
        """幂等预置非生产 Mock 组织树、测试账号及部门管理员范围，不覆盖已有自定义变更。"""
        async with self._session_factory() as session:
            async with session.begin():
                for dept in departments:
                    existing_dept = await session.get(DepartmentModel, dept.id)
                    if existing_dept is None:
                        existing_by_ext = await session.scalar(
                            select(DepartmentModel).where(
                                DepartmentModel.external_id == dept.external_id
                            )
                        )
                        if existing_by_ext is None:
                            session.add(
                                DepartmentModel(
                                    id=dept.id,
                                    external_id=dept.external_id,
                                    parent_id=dept.parent_id,
                                    name=dept.name,
                                    path=dept.path,
                                    order_index=dept.order_index,
                                    status=DepartmentStatus.ACTIVE.value,
                                    synced_at=now,
                                    created_at=now,
                                    updated_at=now,
                                )
                            )
                await session.flush()

                newly_created_user_ids: set[str] = set()
                for user in users:
                    existing_user = await session.get(UserModel, user.id)
                    if existing_user is None:
                        session.add(
                            UserModel(
                                id=user.id,
                                email=user.email,
                                password_hash=user.password_hash,
                                display_name=user.display_name,
                                status=user.status.value,
                                role=user.role.value,
                                auth_provider=user.auth_provider.value,
                                external_union_id=user.external_union_id,
                                external_open_id=user.external_open_id,
                                employee_no=user.employee_no,
                                avatar_url=user.avatar_url,
                                job_title=user.job_title,
                                last_synced_at=now,
                                created_at=user.created_at,
                                updated_at=user.updated_at,
                                deleted_time=user.deleted_time,
                            )
                        )
                        newly_created_user_ids.add(user.id)
                await session.flush()

                for binding in user_departments:
                    if binding.user_id not in newly_created_user_ids:
                        continue
                    existing_binding = await session.get(
                        UserDepartmentModel,
                        (binding.user_id, binding.department_id),
                    )
                    if existing_binding is None:
                        session.add(
                            UserDepartmentModel(
                                user_id=binding.user_id,
                                department_id=binding.department_id,
                                is_primary=binding.is_primary,
                            )
                        )

                for scope in admin_scopes:
                    if scope.user_id not in newly_created_user_ids:
                        continue
                    existing_scope = await session.get(
                        DeptAdminScopeModel,
                        (scope.user_id, scope.department_id),
                    )
                    if existing_scope is None:
                        session.add(
                            DeptAdminScopeModel(
                                user_id=scope.user_id,
                                department_id=scope.department_id,
                                include_subdepartments=scope.include_subdepartments,
                                granted_by=None,
                                created_at=now,
                            )
                        )

    async def apply_feishu_sync(
        self,
        *,
        ordered_departments: Sequence[RemoteDepartment],
        active_employees: Sequence[RemoteEmployee],
        inactive_employees: Sequence[RemoteEmployee],
        now: datetime,
        department_id_factory: Callable[[], str],
        user_id_factory: Callable[[], str],
        actor: AuthContext | None = None,
    ) -> FeishuSyncSummary:
        """在单个数据库事务内原子提交部门树、在职员工归属与离职员工会话吊销。"""
        async with self._session_factory() as session:
            async with session.begin():
                if actor is not None:
                    # 手动触发同步的写事务同样复核超管会话，防止拉取期间超管被降权或撤销。
                    await self._require_super_admin_in_tx(session, actor, now)

                existing_departments = (await session.scalars(select(DepartmentModel))).all()
                non_deleted_users = (
                    await session.scalars(select(UserModel).where(UserModel.deleted_time.is_(None)))
                ).all()

                # 空名单熔断保护：在执行任何归档、停用或会话吊销前校验远端数据完整性，
                # 防止飞书权限误配/临时空列表响应导致存量组织树被清空或全员误吊销。
                has_active_non_mock_department = any(
                    row.status == DepartmentStatus.ACTIVE.value
                    and not row.external_id.startswith(_MOCK_DEPARTMENT_EXTERNAL_PREFIX)
                    for row in existing_departments
                )
                if not ordered_departments and has_active_non_mock_department:
                    raise OrgSyncValidationError("empty_remote_departments")

                has_active_feishu_user = any(
                    row.auth_provider == AuthProvider.FEISHU.value
                    and row.status == UserStatus.ACTIVE.value
                    for row in non_deleted_users
                )
                if not active_employees and not inactive_employees and has_active_feishu_user:
                    raise OrgSyncValidationError("empty_remote_employees")

                existing_by_ext: dict[str, DepartmentModel] = {
                    row.external_id: row for row in existing_departments
                }

                # 1. 为拓扑有序的远端部门分配稳定主键并计算规范物化路径 /{root}/.../{dept_id}/。
                ext_to_local_id: dict[str, str] = {}
                local_id_to_path: dict[str, str] = {}
                remote_ext_ids: set[str] = set()
                for remote in ordered_departments:
                    existing = existing_by_ext.get(remote.external_id)
                    local_id = existing.id if existing is not None else department_id_factory()
                    ext_to_local_id[remote.external_id] = local_id
                    remote_ext_ids.add(remote.external_id)

                departments_upserted = 0
                for remote in ordered_departments:
                    local_id = ext_to_local_id[remote.external_id]
                    parent_local_id = (
                        ext_to_local_id.get(remote.parent_external_id)
                        if remote.parent_external_id is not None
                        else None
                    )
                    if parent_local_id is None:
                        path = f"/{local_id}/"
                    else:
                        path = f"{local_id_to_path[parent_local_id]}{local_id}/"
                    if len(path) > MAX_DEPARTMENT_PATH_LENGTH:
                        raise OrgSyncValidationError("department_path_too_long")
                    local_id_to_path[local_id] = path

                    existing = existing_by_ext.get(remote.external_id)
                    if existing is None:
                        created = DepartmentModel(
                            id=local_id,
                            external_id=remote.external_id,
                            parent_id=parent_local_id,
                            name=remote.name,
                            path=path,
                            order_index=remote.order_index,
                            status=DepartmentStatus.ACTIVE.value,
                            synced_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                        session.add(created)
                        existing_by_ext[remote.external_id] = created
                    else:
                        existing.parent_id = parent_local_id
                        existing.name = remote.name
                        existing.path = path
                        existing.order_index = remote.order_index
                        existing.status = DepartmentStatus.ACTIVE.value
                        existing.synced_at = now
                        existing.updated_at = now
                    departments_upserted += 1
                    await session.flush()

                # 2. 远端已移除的飞书部门置为 archived（跳过非生产 Mock 部门），
                #    不物理删除以保留历史审计与外键完整性。
                departments_archived = 0
                for ext_id, dept_row in existing_by_ext.items():
                    if (
                        ext_id not in remote_ext_ids
                        and not ext_id.startswith(_MOCK_DEPARTMENT_EXTERNAL_PREFIX)
                        and dept_row.status != DepartmentStatus.ARCHIVED.value
                    ):
                        dept_row.status = DepartmentStatus.ARCHIVED.value
                        dept_row.updated_at = now
                        departments_archived += 1
                await session.flush()

                # 3. 基于事务开头读取的未删除用户构建匹配索引，避开邮箱碰撞。
                by_union_id: dict[str, UserModel] = {
                    row.external_union_id: row
                    for row in non_deleted_users
                    if row.external_union_id is not None
                }
                by_open_id: dict[str, UserModel] = {
                    row.external_open_id: row
                    for row in non_deleted_users
                    if row.external_open_id is not None
                }
                email_owner_id: dict[str, str] = {
                    row.email: row.id for row in non_deleted_users if row.email is not None
                }

                # 4. upsert 在职飞书员工并同步其部门归属（严格保留本地已授予的角色与权限）。
                synced_active_user_ids: set[str] = set()
                users_upserted = 0
                for emp in active_employees:
                    matched = self._match_employee_row(emp, by_union_id, by_open_id)
                    if matched is not None and matched.auth_provider != AuthProvider.FEISHU.value:
                        continue

                    normalized_email = _normalize_optional_email(emp.email)
                    if matched is not None:
                        user_row = matched
                        if emp.union_id is not None:
                            user_row.external_union_id = emp.union_id
                            by_union_id[emp.union_id] = user_row
                        if emp.open_id is not None:
                            user_row.external_open_id = emp.open_id
                            by_open_id[emp.open_id] = user_row
                        if emp.name is not None:
                            user_row.display_name = emp.name
                        if normalized_email is not None:
                            current_owner = email_owner_id.get(normalized_email)
                            if current_owner in {None, user_row.id}:
                                old_email = user_row.email
                                if old_email is not None and old_email != normalized_email:
                                    email_owner_id.pop(old_email, None)
                                user_row.email = normalized_email
                                email_owner_id[normalized_email] = user_row.id
                        user_row.employee_no = emp.employee_no
                        user_row.avatar_url = emp.avatar_url
                        user_row.job_title = emp.job_title
                        user_row.status = UserStatus.ACTIVE.value
                        user_row.last_synced_at = now
                        user_row.updated_at = now
                    else:
                        safe_email = normalized_email
                        if safe_email is not None and safe_email in email_owner_id:
                            safe_email = None
                        new_user_id = user_id_factory()
                        user_row = UserModel(
                            id=new_user_id,
                            email=safe_email,
                            password_hash=None,
                            display_name=emp.name,
                            status=UserStatus.ACTIVE.value,
                            role=UserRole.MEMBER.value,
                            auth_provider=AuthProvider.FEISHU.value,
                            external_union_id=emp.union_id,
                            external_open_id=emp.open_id,
                            employee_no=emp.employee_no,
                            avatar_url=emp.avatar_url,
                            job_title=emp.job_title,
                            last_synced_at=now,
                            created_at=now,
                            updated_at=now,
                            deleted_time=None,
                        )
                        session.add(user_row)
                        await session.flush()
                        if emp.union_id is not None:
                            by_union_id[emp.union_id] = user_row
                        if emp.open_id is not None:
                            by_open_id[emp.open_id] = user_row
                        if safe_email is not None:
                            email_owner_id[safe_email] = user_row.id

                    # 替换该员工的部门归属关系：首个命中的活跃部门设为主部门。
                    await session.execute(
                        delete(UserDepartmentModel).where(
                            UserDepartmentModel.user_id == user_row.id
                        )
                    )
                    resolved_dept_ids: list[str] = []
                    for ext_dept_id in emp.department_external_ids:
                        local_dept_id = ext_to_local_id.get(ext_dept_id)
                        if local_dept_id is not None and local_dept_id not in resolved_dept_ids:
                            resolved_dept_ids.append(local_dept_id)
                    for idx, local_dept_id in enumerate(resolved_dept_ids):
                        session.add(
                            UserDepartmentModel(
                                user_id=user_row.id,
                                department_id=local_dept_id,
                                is_primary=(idx == 0),
                            )
                        )
                    synced_active_user_ids.add(user_row.id)
                    users_upserted += 1

                await session.flush()

                # 5. 离职/停用自动吊销：本地既有活跃 feishu 用户不在远端在职花名册中
                # （或被远端标记离职/冻结）时，同事务置为 disabled 并撤销其全部未撤销会话。
                disable_user_ids: list[str] = []
                revoke_session_user_ids: set[str] = set()
                for row in non_deleted_users:
                    if row.auth_provider != AuthProvider.FEISHU.value:
                        continue
                    if row.id in synced_active_user_ids:
                        continue
                    if row.status == UserStatus.ACTIVE.value:
                        row.status = UserStatus.DISABLED.value
                        row.last_synced_at = now
                        row.updated_at = now
                        disable_user_ids.append(row.id)
                        revoke_session_user_ids.add(row.id)

                for inactive_emp in inactive_employees:
                    matched_inactive = self._match_employee_row(
                        inactive_emp,
                        by_union_id,
                        by_open_id,
                    )
                    if (
                        matched_inactive is not None
                        and matched_inactive.auth_provider == AuthProvider.FEISHU.value
                        and matched_inactive.id not in synced_active_user_ids
                    ):
                        if matched_inactive.status != UserStatus.DISABLED.value:
                            matched_inactive.status = UserStatus.DISABLED.value
                            matched_inactive.last_synced_at = now
                            matched_inactive.updated_at = now
                            disable_user_ids.append(matched_inactive.id)
                        revoke_session_user_ids.add(matched_inactive.id)

                sessions_revoked = 0
                if revoke_session_user_ids:
                    revoked_ids = (
                        await session.scalars(
                            update(AuthSessionModel)
                            .where(
                                AuthSessionModel.user_id.in_(sorted(revoke_session_user_ids)),
                                AuthSessionModel.revoked_time.is_(None),
                            )
                            .values(revoked_time=now)
                            .returning(AuthSessionModel.id)
                        )
                    ).all()
                    sessions_revoked = len(revoked_ids)

        return FeishuSyncSummary(
            departments_upserted=departments_upserted,
            departments_archived=departments_archived,
            users_upserted=users_upserted,
            users_disabled=len(disable_user_ids),
            sessions_revoked=sessions_revoked,
            synced_at=now,
        )

    @staticmethod
    def _match_employee_row(
        employee: RemoteEmployee,
        by_union_id: dict[str, UserModel],
        by_open_id: dict[str, UserModel],
    ) -> UserModel | None:
        """优先按 union_id 匹配现有用户，其次回退 open_id。"""
        if employee.union_id is not None and employee.union_id in by_union_id:
            return by_union_id[employee.union_id]
        if employee.open_id is not None and employee.open_id in by_open_id:
            return by_open_id[employee.open_id]
        return None

    @staticmethod
    def _validate_scope_inputs(
        role: UserRole,
        managed_departments: tuple[DeptAdminScopeInput, ...],
    ) -> tuple[DeptAdminScopeInput, ...]:
        """校验角色与管辖部门组合：dept_admin 至少 1 个且不重复；其他角色自动清空。"""
        if role is not UserRole.DEPT_ADMIN:
            return ()
        if not managed_departments:
            raise InvalidDeptAdminScopeError("dept_admin_departments_required")
        seen: set[str] = set()
        cleaned: list[DeptAdminScopeInput] = []
        for item in managed_departments:
            dept_id = item.department_id.strip()
            if not dept_id:
                raise InvalidDeptAdminScopeError("dept_admin_departments_required")
            if dept_id in seen:
                raise InvalidDeptAdminScopeError("duplicate_department_scope")
            seen.add(dept_id)
            cleaned.append(
                DeptAdminScopeInput(
                    department_id=dept_id,
                    include_subdepartments=item.include_subdepartments,
                )
            )
        return tuple(cleaned)

    @staticmethod
    async def _require_super_admin_in_tx(
        session: AsyncSession,
        actor: AuthContext,
        now: datetime,
    ) -> User:
        """在当前写事务中以无锁单条 JOIN 复核操作者仍为有效 super_admin。"""
        current_user = await verify_active_session_user(
            session,
            actor.user.id,
            actor.session_id,
            clock=lambda: now,
        )
        if current_user is None or current_user.role is not UserRole.SUPER_ADMIN:
            raise OrgAuthorizationError("admin_required")
        return current_user

    @staticmethod
    async def _ensure_another_active_super_admin(
        session: AsyncSession,
        excluding_user_id: str,
    ) -> None:
        """锁定全部活跃 super_admin 行，确保除去目标账号外至少还存在 1 位活跃超管。"""
        active_super_admins = (
            await session.scalars(
                select(UserModel.id)
                .where(
                    UserModel.role == UserRole.SUPER_ADMIN.value,
                    UserModel.status == UserStatus.ACTIVE.value,
                    UserModel.deleted_time.is_(None),
                )
                .order_by(UserModel.id.asc())
                .with_for_update()
            )
        ).all()
        remaining = [uid for uid in active_super_admins if uid != excluding_user_id]
        if not remaining:
            raise LastSuperAdminProtectedError("last_super_admin_protected")

    @staticmethod
    async def _load_org_relations(
        session: AsyncSession,
        user_ids: Sequence[str],
    ) -> tuple[
        dict[str, tuple[UserDepartmentSummary, ...]],
        dict[str, tuple[DeptAdminScopeDetail, ...]],
    ]:
        """批量加载一批用户的活跃部门归属与部门管理员管辖范围，避免 N+1 查询。"""
        if not user_ids:
            return {}, {}

        dept_rows = (
            await session.execute(
                select(
                    UserDepartmentModel.user_id,
                    DepartmentModel.id,
                    DepartmentModel.name,
                    DepartmentModel.path,
                    UserDepartmentModel.is_primary,
                )
                .join(
                    DepartmentModel,
                    DepartmentModel.id == UserDepartmentModel.department_id,
                )
                .where(
                    UserDepartmentModel.user_id.in_(user_ids),
                    DepartmentModel.status == DepartmentStatus.ACTIVE.value,
                )
                .order_by(
                    UserDepartmentModel.is_primary.desc(),
                    DepartmentModel.path.asc(),
                    DepartmentModel.order_index.asc(),
                    DepartmentModel.id.asc(),
                )
            )
        ).all()
        dept_grouped: dict[str, list[UserDepartmentSummary]] = {}
        for uid, dept_id, dept_name, dept_path, is_primary in dept_rows:
            dept_grouped.setdefault(uid, []).append(
                UserDepartmentSummary(
                    id=dept_id,
                    name=dept_name,
                    path=dept_path,
                    is_primary=bool(is_primary),
                )
            )

        scope_rows = (
            await session.execute(
                select(
                    DeptAdminScopeModel.user_id,
                    DepartmentModel.id,
                    DepartmentModel.name,
                    DepartmentModel.path,
                    DeptAdminScopeModel.include_subdepartments,
                )
                .join(
                    DepartmentModel,
                    DepartmentModel.id == DeptAdminScopeModel.department_id,
                )
                .where(
                    DeptAdminScopeModel.user_id.in_(user_ids),
                    DepartmentModel.status == DepartmentStatus.ACTIVE.value,
                )
                .order_by(
                    DepartmentModel.path.asc(),
                    DepartmentModel.order_index.asc(),
                    DepartmentModel.id.asc(),
                )
            )
        ).all()
        scope_grouped: dict[str, list[DeptAdminScopeDetail]] = {}
        for uid, dept_id, dept_name, dept_path, include_sub in scope_rows:
            scope_grouped.setdefault(uid, []).append(
                DeptAdminScopeDetail(
                    department_id=dept_id,
                    department_name=dept_name,
                    department_path=dept_path,
                    include_subdepartments=bool(include_sub),
                )
            )

        return (
            {uid: tuple(items) for uid, items in dept_grouped.items()},
            {uid: tuple(items) for uid, items in scope_grouped.items()},
        )

    @staticmethod
    def _build_user_detail(
        row: UserModel,
        dept_map: dict[str, tuple[UserDepartmentSummary, ...]],
        scope_map: dict[str, tuple[DeptAdminScopeDetail, ...]],
    ) -> OrgUserDetail:
        """组合用户领域对象、部门摘要与管辖范围详情。"""
        scopes = scope_map.get(row.id, ())
        return OrgUserDetail(
            user=user_to_domain(row),
            departments=dept_map.get(row.id, ()),
            managed_departments=scopes,
            managed_department_ids=tuple(scope.department_id for scope in scopes),
        )


__all__ = ["SqlAlchemyOrgRepository", "department_to_domain"]
