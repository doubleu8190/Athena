"""Skill 管理目录、Run 快照与脚本附加记录的 SQLAlchemy 仓储。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.access.repository import (
    delete_resource_grants_in_session,
    filter_readable_targets_in_session,
    load_grants_map_in_session,
    load_user_access_snapshot_in_session,
    sync_resource_grants_in_session,
)
from adapter.auth.repository.transaction_guard import SqlAlchemyAuthTransactionGuard
from adapter.run.model import RunToolCallModel
from adapter.skill.converter import (
    skill_execution_to_domain,
    skill_to_domain,
    skill_version_to_domain,
)
from adapter.skill.model import SkillExecutionModel, SkillModel, SkillVersionModel
from domain.access import (
    ResourceAccessTarget,
    ResourceType,
    VisibilityGrantInput,
    VisibilityScope,
    evaluate_can_manage,
)
from domain.auth import AuthContext, UserRole
from domain.file import PendingSessionFile
from domain.skill.model import (
    DependencyCheck,
    DependencyCheckStatus,
    RunSkillEntry,
    RunSkillSnapshot,
    Skill,
    SkillConflictError,
    SkillExecutionArtifact,
    SkillExecutionRecord,
    SkillExecutionStart,
    SkillExecutionStatus,
    SkillImportRequestResult,
    SkillNotFoundError,
    SkillPackageManifest,
    SkillScope,
    SkillVersion,
    SkillVersionPage,
)
from domain.skill.ports import SkillRepository

_PERSONAL_REQUEST_OPERATIONS = frozenset({"personal_create", "personal_publish"})
_ADMIN_MANAGED_SCOPES = (SkillScope.COMPANY.value, SkillScope.DEPARTMENT.value)


async def build_run_skill_snapshot(
    session: AsyncSession,
    selected_skill_ids: tuple[str, ...],
    *,
    owner_id: str,
    fallback_selected_skill_ids: tuple[str, ...] = (),
) -> RunSkillSnapshot:
    """在当前 Run 事务中按统一三层权限引擎实时过滤并固定一致 Skill 目录。

    - selected_skill_ids：用户显式指定，任一不可用/无权均抛出 SkillConflictError；
    - fallback_selected_skill_ids：仅在用户未显式指定时生效（如项目默认技能），
      只保留当前用户仍可读且已启用的子集，失效项自动静默忽略。
    """
    if len(selected_skill_ids) != len(set(selected_skill_ids)):
        raise SkillConflictError("skill_selection_invalid")
    rows = (
        await session.execute(
            select(SkillModel, SkillVersionModel)
            .join(
                SkillVersionModel,
                SkillVersionModel.id == SkillModel.current_version_id,
            )
            .where(
                SkillModel.enabled.is_(True),
                SkillModel.deleted_at.is_(None),
                or_(
                    SkillModel.scope.in_(_ADMIN_MANAGED_SCOPES),
                    and_(
                        SkillModel.scope == SkillScope.PERSONAL.value,
                        SkillModel.owner_id == owner_id,
                    ),
                ),
            )
            .order_by(SkillModel.scope, SkillModel.slug, SkillModel.id)
        )
    ).all()

    # 同一事务快照内通过统一权限引擎实时过滤 department/company/personal 可读技能，
    # 员工调岗后无法将旧部门技能选入新 Run。
    targets = tuple(
        ResourceAccessTarget(
            resource_type=ResourceType.SKILL,
            resource_id=skill_row.id,
            visibility_scope=VisibilityScope(skill_row.scope),
            owner_id=skill_row.owner_id,
            owner_department_id=skill_row.owner_department_id,
        )
        for skill_row, _ in rows
    )
    readable_ids = await filter_readable_targets_in_session(session, owner_id, targets)

    available: list[tuple[SkillModel, SkillVersionModel]] = []
    for skill_row, version_row in rows:
        if skill_row.id not in readable_ids:
            continue
        # 持久化 dependency_check 只是上次探测快照，不能成为新 Run 的永久门禁；
        # 实际脚本执行前仍会实时复核，这里只验证目录和不可变清单完整性。
        # 清单解析失败说明不可变记录损坏，不能让 Run 以不完整目录继续。
        version = skill_version_to_domain(version_row)
        scope = SkillScope(skill_row.scope)
        if scope is SkillScope.PERSONAL and (
            version.dependency_requirements
            or version.manifest.scripts
            or skill_row.script_requires_approval
        ):
            raise SkillConflictError("skill_catalog_integrity_invalid")
        available.append((skill_row, version_row))
        if version.manifest.slug != skill_row.slug:
            raise SkillConflictError("skill_catalog_integrity_invalid")
    available_ids = {skill.id for skill, _ in available}
    if selected_skill_ids:
        if not set(selected_skill_ids) <= available_ids:
            raise SkillConflictError("skill_selection_unavailable")
        selected_set = set(selected_skill_ids)
    else:
        # 项目默认技能仅做安全交集过滤，失效/已删/无权项自动静默忽略，绝不阻断对话。
        selected_set = set(fallback_selected_skill_ids) & available_ids
    catalog = tuple(
        RunSkillEntry(
            skill_id=skill.id,
            slug=skill.slug,
            name=SkillPackageManifest.model_validate(version.manifest).name,
            description=SkillPackageManifest.model_validate(version.manifest).description,
            version_id=version.id,
            version_label=version.label,
            requires_approval=skill.script_requires_approval,
            scope=SkillScope(skill.scope),
        )
        for skill, version in available
    )
    selected_in_catalog = tuple(item.skill_id for item in catalog if item.skill_id in selected_set)
    return RunSkillSnapshot(
        skills=catalog,
        selected_skill_ids=selected_in_catalog,
    )


class SqlAlchemySkillRepository(SkillRepository):
    """在管理员事务内维护稳定身份、不可变版本指针与三层可见性授权。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """保存共享数据库会话工厂。"""
        self._session_factory = session_factory

    async def list_skills(self) -> tuple[Skill, ...]:
        """按 slug 稳定读取全部未删除公司级与部门级管理目录项（含可见性授权）。"""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(SkillModel, SkillVersionModel)
                    .outerjoin(
                        SkillVersionModel,
                        SkillVersionModel.id == SkillModel.current_version_id,
                    )
                    .where(
                        SkillModel.scope.in_(_ADMIN_MANAGED_SCOPES),
                        SkillModel.deleted_at.is_(None),
                    )
                    .order_by(SkillModel.slug, SkillModel.id)
                )
            ).all()
            grants_map = await load_grants_map_in_session(
                session, ResourceType.SKILL, [row.id for row, _ in rows]
            )
        return tuple(
            skill_to_domain(row, version, grants=grants_map.get(row.id, ()))
            for row, version in rows
        )

    async def get_skill(self, skill_id: str) -> Skill | None:
        """读取一个未删除的公司级或部门级管理目录项及其当前版本与授权。"""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(SkillModel, SkillVersionModel)
                    .outerjoin(
                        SkillVersionModel,
                        SkillVersionModel.id == SkillModel.current_version_id,
                    )
                    .where(
                        SkillModel.id == skill_id,
                        SkillModel.scope.in_(_ADMIN_MANAGED_SCOPES),
                        SkillModel.deleted_at.is_(None),
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            grants_map = await load_grants_map_in_session(session, ResourceType.SKILL, (row[0].id,))
            return skill_to_domain(row[0], row[1], grants=grants_map.get(row[0].id, ()))

    async def get_public_directory_entries(self) -> tuple[tuple[Skill, SkillVersion], ...]:
        """单次 JOIN 读取全部已启用公司级候选及其当前不可变版本。"""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(SkillModel, SkillVersionModel)
                    .join(
                        SkillVersionModel,
                        SkillVersionModel.id == SkillModel.current_version_id,
                    )
                    .where(
                        SkillModel.enabled.is_(True),
                        SkillModel.scope == SkillScope.COMPANY.value,
                        SkillModel.deleted_at.is_(None),
                    )
                    .order_by(SkillModel.slug, SkillModel.id)
                )
            ).all()
        return tuple(
            (skill_to_domain(row, version), skill_version_to_domain(version))
            for row, version in rows
        )

    async def get_public_directory(self) -> tuple[Skill, ...]:
        """读取全部已启用公司级候选，依赖状态由服务层实时探测。"""
        entries = await self.get_public_directory_entries()
        return tuple(skill for skill, _ in entries)

    async def get_user_directory_entries(
        self, owner_id: str
    ) -> tuple[tuple[Skill, SkillVersion], ...]:
        """单次 JOIN 读取公司级、部门级及本人已启用候选及其当前版本，供权限引擎实时过滤。"""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(SkillModel, SkillVersionModel)
                    .join(
                        SkillVersionModel,
                        SkillVersionModel.id == SkillModel.current_version_id,
                    )
                    .where(
                        SkillModel.enabled.is_(True),
                        SkillModel.deleted_at.is_(None),
                        or_(
                            SkillModel.scope.in_(_ADMIN_MANAGED_SCOPES),
                            and_(
                                SkillModel.scope == SkillScope.PERSONAL.value,
                                SkillModel.owner_id == owner_id,
                            ),
                        ),
                    )
                    .order_by(SkillModel.scope, SkillModel.slug, SkillModel.id)
                )
            ).all()
            grants_map = await load_grants_map_in_session(
                session, ResourceType.SKILL, [row.id for row, _ in rows]
            )
        return tuple(
            (
                skill_to_domain(row, version, grants=grants_map.get(row.id, ())),
                skill_version_to_domain(version),
            )
            for row, version in rows
        )

    async def get_user_directory(self, owner_id: str) -> tuple[Skill, ...]:
        """读取公共和本人已启用且未删除的 Skill 候选。"""
        entries = await self.get_user_directory_entries(owner_id)
        return tuple(skill for skill, _ in entries)

    async def list_personal_skills(self, owner_id: str) -> tuple[Skill, ...]:
        """读取一人未删除的个人 Skill，包含停用项。"""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(SkillModel, SkillVersionModel)
                    .outerjoin(
                        SkillVersionModel,
                        SkillVersionModel.id == SkillModel.current_version_id,
                    )
                    .where(
                        SkillModel.scope == SkillScope.PERSONAL.value,
                        SkillModel.owner_id == owner_id,
                        SkillModel.deleted_at.is_(None),
                    )
                    .order_by(SkillModel.updated_at.desc(), SkillModel.id)
                )
            ).all()
        return tuple(skill_to_domain(row, version) for row, version in rows)

    async def get_personal_skill(self, owner_id: str, skill_id: str) -> Skill | None:
        """只在本人、个人范围且未删除时读取 Skill 详情。"""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(SkillModel, SkillVersionModel)
                    .outerjoin(
                        SkillVersionModel,
                        SkillVersionModel.id == SkillModel.current_version_id,
                    )
                    .where(
                        SkillModel.id == skill_id,
                        SkillModel.scope == SkillScope.PERSONAL.value,
                        SkillModel.owner_id == owner_id,
                        SkillModel.deleted_at.is_(None),
                    )
                )
            ).one_or_none()
        return None if row is None else skill_to_domain(row[0], row[1])

    async def list_versions(
        self, skill_id: str, *, page: int, page_size: int
    ) -> SkillVersionPage | None:
        """分页读取不可变版本历史。"""
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError("skill_version_page_invalid")
        async with self._session_factory() as session:
            exists = await session.scalar(
                select(SkillModel.id).where(
                    SkillModel.id == skill_id,
                    SkillModel.scope.in_(_ADMIN_MANAGED_SCOPES),
                    SkillModel.deleted_at.is_(None),
                )
            )
            if exists is None:
                return None
            total = int(
                (
                    await session.scalar(
                        select(func.count(SkillVersionModel.id)).where(
                            SkillVersionModel.skill_id == skill_id
                        )
                    )
                )
                or 0
            )
            rows = (
                await session.scalars(
                    select(SkillVersionModel)
                    .where(SkillVersionModel.skill_id == skill_id)
                    .order_by(
                        SkillVersionModel.ordinal.desc(),
                        SkillVersionModel.id.desc(),
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
        return SkillVersionPage(
            items=tuple(skill_version_to_domain(row) for row in rows),
            total=total,
        )

    async def get_version(self, version_id: str) -> SkillVersion | None:
        """读取任意不可变版本。"""
        async with self._session_factory() as session:
            row = await session.get(SkillVersionModel, version_id)
        return None if row is None else skill_version_to_domain(row)

    async def create_skill(
        self,
        *,
        skill_id: str,
        version_id: str,
        label: str,
        manifest: SkillPackageManifest,
        object_key: str,
        package_sha256: str,
        dependency_check: DependencyCheck,
        actor: AuthContext,
        idempotency_key: str,
        request_fingerprint: str,
        created_at: datetime,
        scope: SkillScope = SkillScope.COMPANY,
        owner_department_id: str | None = None,
        visibility_grants: Sequence[VisibilityGrantInput] = (),
    ) -> tuple[Skill, SkillVersion]:
        """原子插入身份、首个未启用版本及可见性授权；事务内复核管理员与管辖范围。"""
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._require_skill_manager(
                        session,
                        actor,
                        scope=scope,
                        owner_department_id=owner_department_id,
                    )
                    skill_row = SkillModel(
                        id=skill_id,
                        slug=manifest.slug,
                        scope=scope.value,
                        owner_id=None,
                        owner_department_id=owner_department_id,
                        deleted_at=None,
                        current_version_id=None,
                        enabled=False,
                        script_requires_approval=False,
                        revision=1,
                        created_by=actor.user.id,
                        created_at=created_at,
                        updated_at=created_at,
                    )
                    session.add(skill_row)
                    await session.flush()
                    version_row = self._version_model(
                        version_id=version_id,
                        skill_id=skill_id,
                        ordinal=1,
                        label=label,
                        manifest=manifest,
                        object_key=object_key,
                        package_sha256=package_sha256,
                        dependency_check=dependency_check,
                        created_by=actor.user.id,
                        request_operation="import",
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        request_target_skill_id=None,
                        created_at=created_at,
                    )
                    session.add(version_row)
                    await session.flush()
                    grants = await sync_resource_grants_in_session(
                        session,
                        ResourceType.SKILL,
                        skill_id,
                        visibility_grants if scope is SkillScope.DEPARTMENT else (),
                        now=created_at,
                    )
                    return (
                        skill_to_domain(skill_row, version_row, grants=grants),
                        skill_version_to_domain(version_row),
                    )
        except IntegrityError as error:
            replay = await self._read_import_result(
                actor,
                operation="import",
                target_skill_id=None,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                return replay.skill, replay.version
            raise SkillConflictError("skill_slug_exists") from error

    async def create_personal_skill(
        self,
        *,
        skill_id: str,
        version_id: str,
        manifest: SkillPackageManifest,
        object_key: str,
        package_sha256: str,
        dependency_check: DependencyCheck,
        actor: AuthContext,
        idempotency_key: str,
        request_fingerprint: str,
        created_at: datetime,
    ) -> SkillImportRequestResult:
        """原子创建本人 Skill、首版和启用指针。"""
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._require_active_user(session, actor)
                    skill_row = SkillModel(
                        id=skill_id,
                        slug=manifest.slug,
                        scope=SkillScope.PERSONAL.value,
                        owner_id=actor.user.id,
                        deleted_at=None,
                        current_version_id=None,
                        enabled=True,
                        script_requires_approval=False,
                        revision=1,
                        created_by=actor.user.id,
                        created_at=created_at,
                        updated_at=created_at,
                    )
                    version_row = self._version_model(
                        version_id=version_id,
                        skill_id=skill_id,
                        ordinal=1,
                        label="v1",
                        manifest=manifest,
                        object_key=object_key,
                        package_sha256=package_sha256,
                        dependency_check=dependency_check,
                        created_by=actor.user.id,
                        request_operation="personal_create",
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        request_target_skill_id=None,
                        created_at=created_at,
                    )
                    session.add(skill_row)
                    await session.flush()
                    session.add(version_row)
                    await session.flush()
                    skill_row.current_version_id = version_id
                    await session.flush()
                    request_version = skill_version_to_domain(version_row)
                    return SkillImportRequestResult(
                        skill=skill_to_domain(skill_row, version_row),
                        version=request_version,
                        request_version=request_version,
                    )
        except IntegrityError as error:
            replay = await self._read_import_result(
                actor,
                operation="personal_create",
                target_skill_id=None,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                return replay
            raise SkillConflictError("skill_personal_create_conflict") from error

    async def publish_personal_version(
        self,
        skill_id: str,
        *,
        version_id: str,
        expected_revision: int,
        manifest: SkillPackageManifest,
        object_key: str,
        package_sha256: str,
        dependency_check: DependencyCheck,
        actor: AuthContext,
        idempotency_key: str,
        request_fingerprint: str,
        created_at: datetime,
    ) -> SkillImportRequestResult | None:
        """追加个人版本，并在同一事务中切换指针、启用和修订号。"""
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._require_active_user(session, actor)
                    skill_row = await session.scalar(
                        select(SkillModel)
                        .where(
                            SkillModel.id == skill_id,
                            SkillModel.scope == SkillScope.PERSONAL.value,
                            SkillModel.owner_id == actor.user.id,
                            SkillModel.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                    if skill_row is None:
                        return None
                    # 同一幂等键的并发请求在拿到身份锁后先重放胜者，
                    # 不能因为胜者已递增 revision 而误报 CAS 冲突。
                    replay = await self._read_import_result_in_session(
                        session,
                        actor,
                        operation="personal_publish",
                        target_skill_id=skill_id,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                    )
                    if replay is not None:
                        return replay
                    if skill_row.revision != expected_revision:
                        raise SkillConflictError("skill_revision_conflict")
                    if skill_row.slug != manifest.slug:
                        raise SkillConflictError("skill_personal_integrity_invalid")
                    ordinal = (
                        int(
                            (
                                await session.scalar(
                                    select(func.max(SkillVersionModel.ordinal)).where(
                                        SkillVersionModel.skill_id == skill_id
                                    )
                                )
                            )
                            or 0
                        )
                        + 1
                    )
                    version_row = self._version_model(
                        version_id=version_id,
                        skill_id=skill_id,
                        ordinal=ordinal,
                        label=f"v{ordinal}",
                        manifest=manifest,
                        object_key=object_key,
                        package_sha256=package_sha256,
                        dependency_check=dependency_check,
                        created_by=actor.user.id,
                        request_operation="personal_publish",
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        request_target_skill_id=skill_id,
                        created_at=created_at,
                    )
                    session.add(version_row)
                    await session.flush()
                    skill_row.current_version_id = version_id
                    skill_row.enabled = True
                    skill_row.revision += 1
                    skill_row.updated_at = created_at
                    await session.flush()
                    request_version = skill_version_to_domain(version_row)
                    return SkillImportRequestResult(
                        skill=skill_to_domain(skill_row, version_row),
                        version=request_version,
                        request_version=request_version,
                    )
        except IntegrityError as error:
            replay = await self._read_import_result(
                actor,
                operation="personal_publish",
                target_skill_id=skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                return replay
            raise SkillConflictError("skill_personal_publish_conflict") from error

    async def add_version(
        self,
        skill_id: str,
        *,
        version_id: str,
        label: str | None,
        manifest: SkillPackageManifest,
        object_key: str,
        package_sha256: str,
        dependency_check: DependencyCheck,
        actor: AuthContext,
        idempotency_key: str,
        request_fingerprint: str,
        created_at: datetime,
    ) -> SkillVersion | None:
        """追加完整新版本，不改变当前指针、启用状态或审批配置。"""
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._require_skill_manager_session(session, actor)
                    skill_row = await session.scalar(
                        select(SkillModel)
                        .where(
                            SkillModel.id == skill_id,
                            SkillModel.scope.in_(_ADMIN_MANAGED_SCOPES),
                            SkillModel.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                    if skill_row is None:
                        return None
                    await self._require_skill_manager(
                        session,
                        actor,
                        scope=SkillScope(skill_row.scope),
                        owner_department_id=skill_row.owner_department_id,
                    )
                    if skill_row.slug != manifest.slug:
                        raise SkillConflictError("skill_slug_mismatch")
                    ordinal = (
                        int(
                            (
                                await session.scalar(
                                    select(func.max(SkillVersionModel.ordinal)).where(
                                        SkillVersionModel.skill_id == skill_id
                                    )
                                )
                            )
                            or 0
                        )
                        + 1
                    )
                    version_row = self._version_model(
                        version_id=version_id,
                        skill_id=skill_id,
                        ordinal=ordinal,
                        label=label or f"v{ordinal}",
                        manifest=manifest,
                        object_key=object_key,
                        package_sha256=package_sha256,
                        dependency_check=dependency_check,
                        created_by=actor.user.id,
                        request_operation="add_version",
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        request_target_skill_id=skill_id,
                        created_at=created_at,
                    )
                    session.add(version_row)
                    await session.flush()
                    return skill_version_to_domain(version_row)
        except IntegrityError as error:
            replay = await self._read_import_result(
                actor,
                operation="add_version",
                target_skill_id=skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                return replay.version
            raise SkillConflictError("skill_version_conflict") from error

    async def find_import_result(
        self,
        actor: AuthContext,
        *,
        operation: str,
        target_skill_id: str | None,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> SkillImportRequestResult | None:
        """在写对象前查询导入请求键，网络重试不会生成第二个版本。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_skill_manager_session(session, actor)
                return await self._read_import_result_in_session(
                    session,
                    actor,
                    operation=operation,
                    target_skill_id=target_skill_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )

    async def find_personal_request_result(
        self,
        actor: AuthContext,
        *,
        operation: str,
        target_skill_id: str | None,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> SkillImportRequestResult | None:
        """在本人有效登录事务内读取个人创建或发布幂等结果。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_active_user(session, actor)
                result = await self._read_import_result_in_session(
                    session,
                    actor,
                    operation=operation,
                    target_skill_id=target_skill_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )
                if result is not None and (
                    result.skill.scope is not SkillScope.PERSONAL
                    or result.skill.owner_id != actor.user.id
                ):
                    raise SkillConflictError("skill_personal_integrity_invalid")
                if result is not None and result.skill.deleted_at is not None:
                    raise SkillNotFoundError()
                return result

    async def update_configuration(
        self,
        skill_id: str,
        *,
        expected_revision: int,
        enabled: bool,
        current_version_id: str,
        script_requires_approval: bool,
        dependency_check: DependencyCheck,
        actor: AuthContext,
        updated_at: datetime,
        visibility_grants: Sequence[VisibilityGrantInput] | None = None,
    ) -> Skill | None:
        """以修订号 CAS 原子切换版本、启停、审批及部门可见性授权。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_skill_manager_session(session, actor)
                skill_row = await session.scalar(
                    select(SkillModel)
                    .where(
                        SkillModel.id == skill_id,
                        SkillModel.scope.in_(_ADMIN_MANAGED_SCOPES),
                        SkillModel.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
                if skill_row is None:
                    return None
                skill_scope = SkillScope(skill_row.scope)
                await self._require_skill_manager(
                    session,
                    actor,
                    scope=skill_scope,
                    owner_department_id=skill_row.owner_department_id,
                )
                if skill_row.revision != expected_revision:
                    raise SkillConflictError("skill_revision_conflict")
                version_row = await session.scalar(
                    select(SkillVersionModel).where(
                        SkillVersionModel.id == current_version_id,
                        SkillVersionModel.skill_id == skill_id,
                    )
                )
                if version_row is None:
                    raise SkillNotFoundError("skill_version_not_found")
                if enabled and dependency_check.status is not DependencyCheckStatus.SATISFIED:
                    raise ValueError("skill_dependency_unsatisfied")
                version_row.dependency_check = dependency_check.model_dump(mode="json")
                grants_changed = visibility_grants is not None
                if (
                    skill_row.current_version_id != current_version_id
                    or skill_row.enabled != enabled
                    or skill_row.script_requires_approval != script_requires_approval
                    or grants_changed
                ):
                    skill_row.current_version_id = current_version_id
                    skill_row.enabled = enabled
                    skill_row.script_requires_approval = script_requires_approval
                    skill_row.revision += 1
                    skill_row.updated_at = updated_at
                    await session.flush()
                if visibility_grants is not None:
                    grants = await sync_resource_grants_in_session(
                        session,
                        ResourceType.SKILL,
                        skill_row.id,
                        visibility_grants if skill_scope is SkillScope.DEPARTMENT else (),
                        now=updated_at,
                    )
                else:
                    grants_map = await load_grants_map_in_session(
                        session, ResourceType.SKILL, (skill_row.id,)
                    )
                    grants = grants_map.get(skill_row.id, ())
                return skill_to_domain(skill_row, version_row, grants=grants)

    async def delete_admin_skill(
        self,
        skill_id: str,
        *,
        expected_revision: int,
        actor: AuthContext,
        deleted_at: datetime,
    ) -> bool:
        """在单事务内复核权限并按 revision 软删除部门级 Skill 及关联授权。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_skill_manager_session(session, actor)
                skill_row = await session.scalar(
                    select(SkillModel)
                    .where(
                        SkillModel.id == skill_id,
                        SkillModel.scope.in_(_ADMIN_MANAGED_SCOPES),
                    )
                    .with_for_update()
                )
                if skill_row is None:
                    raise SkillNotFoundError()
                skill_scope = SkillScope(skill_row.scope)
                await self._require_skill_manager(
                    session,
                    actor,
                    scope=skill_scope,
                    owner_department_id=skill_row.owner_department_id,
                )
                if skill_scope is SkillScope.COMPANY:
                    raise ValueError("company_skill_delete_unsupported")
                if skill_row.revision != expected_revision:
                    raise SkillConflictError("skill_revision_conflict")
                if skill_row.deleted_at is not None:
                    return True
                skill_row.enabled = False
                skill_row.deleted_at = deleted_at
                skill_row.updated_at = deleted_at
                await delete_resource_grants_in_session(session, ResourceType.SKILL, skill_row.id)
                await session.flush()
                return True

    async def set_personal_enabled(
        self,
        skill_id: str,
        *,
        expected_revision: int,
        enabled: bool,
        actor: AuthContext,
        updated_at: datetime,
    ) -> Skill | None:
        """在 owner 和个人范围锁内 CAS 更新启停。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_active_user(session, actor)
                skill_row = await session.scalar(
                    select(SkillModel)
                    .where(
                        SkillModel.id == skill_id,
                        SkillModel.scope == SkillScope.PERSONAL.value,
                        SkillModel.owner_id == actor.user.id,
                        SkillModel.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
                if skill_row is None:
                    return None
                if skill_row.revision != expected_revision:
                    raise SkillConflictError("skill_revision_conflict")
                version_row = await session.get(
                    SkillVersionModel,
                    skill_row.current_version_id,
                )
                if version_row is None or version_row.skill_id != skill_id:
                    raise SkillNotFoundError("skill_version_unavailable")
                if skill_row.enabled != enabled:
                    skill_row.enabled = enabled
                    skill_row.revision += 1
                    skill_row.updated_at = updated_at
                    await session.flush()
                return skill_to_domain(skill_row, version_row)

    async def delete_personal_skill(
        self,
        skill_id: str,
        *,
        expected_revision: int,
        actor: AuthContext,
        deleted_at: datetime,
    ) -> bool:
        """按 owner 与 revision 软删除个人身份，保留全部版本。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_active_user(session, actor)
                skill_row = await session.scalar(
                    select(SkillModel)
                    .where(
                        SkillModel.id == skill_id,
                        SkillModel.scope == SkillScope.PERSONAL.value,
                        SkillModel.owner_id == actor.user.id,
                    )
                    .with_for_update()
                )
                if skill_row is None:
                    raise SkillNotFoundError()
                if skill_row.revision != expected_revision:
                    raise SkillConflictError("skill_revision_conflict")
                if skill_row.deleted_at is not None:
                    return True
                skill_row.deleted_at = deleted_at
                skill_row.updated_at = deleted_at
                await session.flush()
                return True

    async def record_dependency_check(
        self,
        version_id: str,
        dependency_check: DependencyCheck,
    ) -> SkillVersion | None:
        """刷新依赖检查记录，不修改 manifest、对象键或包摘要。"""
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(SkillVersionModel)
                    .where(SkillVersionModel.id == version_id)
                    .with_for_update()
                )
                if row is None:
                    return None
                row.dependency_check = dependency_check.model_dump(mode="json")
                await session.flush()
                return skill_version_to_domain(row)

    async def start_script_execution(
        self,
        *,
        execution_id: str,
        run_id: str,
        session_id: str,
        tool_call_record_id: str,
        skill_id: str,
        skill_version_id: str,
        script_path: str,
        input_file_ids: tuple[str, ...],
        parameters_sha256: str,
        python_version: str,
        dependency_check: DependencyCheck,
        created_at: datetime,
    ) -> SkillExecutionStart | None:
        """以工具调用唯一约束保证同一调用只有一条脚本附加记录。"""
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    call = await session.scalar(
                        select(RunToolCallModel).where(
                            RunToolCallModel.id == tool_call_record_id,
                            RunToolCallModel.run_id == run_id,
                            RunToolCallModel.session_id == session_id,
                        )
                    )
                    if call is None:
                        raise SkillNotFoundError("skill_tool_call_not_found")
                    existing = await session.scalar(
                        select(SkillExecutionModel)
                        .where(
                            SkillExecutionModel.tool_call_record_id
                            == tool_call_record_id
                        )
                        .with_for_update()
                    )
                    if existing is not None:
                        return self._existing_execution_start(
                            existing,
                            run_id=run_id,
                            session_id=session_id,
                            skill_id=skill_id,
                            skill_version_id=skill_version_id,
                            script_path=script_path,
                            input_file_ids=input_file_ids,
                            parameters_sha256=parameters_sha256,
                        )
                    row = SkillExecutionModel(
                        id=execution_id,
                        run_id=run_id,
                        session_id=session_id,
                        tool_call_record_id=tool_call_record_id,
                        skill_id=skill_id,
                        skill_version_id=skill_version_id,
                        script_path=script_path,
                        input_file_ids=list(input_file_ids),
                        parameters_sha256=parameters_sha256,
                        python_version=python_version,
                        dependency_check=dependency_check.model_dump(mode="json"),
                        status=SkillExecutionStatus.RUNNING.value,
                        log_object_key=None,
                        log_truncated=False,
                        log_error_code=None,
                        error_code=None,
                        cleanup_error_code=None,
                        result_artifacts=[],
                        created_at=created_at,
                        finished_at=None,
                    )
                    session.add(row)
                    await session.flush()
                    return SkillExecutionStart(
                        record=skill_execution_to_domain(row),
                        created=True,
                    )
        except IntegrityError:
            # 两个并发请求可能同时通过“无记录”检查；唯一约束只会放行一个，
            # 败者必须在事务回滚后重读胜者，绝不能把插入失败当成可重试的新执行。
            async with self._session_factory() as session:
                existing = await session.scalar(
                    select(SkillExecutionModel).where(
                        SkillExecutionModel.tool_call_record_id == tool_call_record_id
                    )
                )
            if existing is None:
                # 不是同一工具调用的并发重复；保留原始完整性错误供上层定位。
                raise
            return self._existing_execution_start(
                existing,
                run_id=run_id,
                session_id=session_id,
                skill_id=skill_id,
                skill_version_id=skill_version_id,
                script_path=script_path,
                input_file_ids=input_file_ids,
                parameters_sha256=parameters_sha256,
            )

    @staticmethod
    def _existing_execution_start(
        existing: SkillExecutionModel,
        *,
        run_id: str,
        session_id: str,
        skill_id: str,
        skill_version_id: str,
        script_path: str,
        input_file_ids: tuple[str, ...],
        parameters_sha256: str,
    ) -> SkillExecutionStart:
        """校验既有记录与本次请求一致，并明确标记为非本次新建。"""
        matches = (
            existing.run_id == run_id
            and existing.session_id == session_id
            and existing.skill_id == skill_id
            and existing.skill_version_id == skill_version_id
            and existing.script_path == script_path
            and tuple(existing.input_file_ids) == input_file_ids
            and existing.parameters_sha256 == parameters_sha256
        )
        if not matches:
            raise SkillConflictError("skill_execution_request_conflict")
        return SkillExecutionStart(
            record=skill_execution_to_domain(existing),
            created=False,
        )

    async def finish_script_execution(
        self,
        execution_id: str,
        *,
        status: SkillExecutionStatus,
        log_object_key: str | None,
        log_truncated: bool,
        log_error_code: str | None,
        error_code: str | None,
        cleanup_error_code: str | None,
        artifacts: Sequence[PendingSessionFile],
        finished_at: datetime,
    ) -> SkillExecutionRecord | None:
        """保存脚本终态；已完成的相同调用幂等返回原记录。"""
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(SkillExecutionModel)
                    .where(SkillExecutionModel.id == execution_id)
                    .with_for_update()
                )
                if row is None:
                    return None
                if row.status != SkillExecutionStatus.RUNNING.value:
                    return skill_execution_to_domain(row)
                row.status = status.value
                row.log_object_key = log_object_key
                row.log_truncated = log_truncated
                row.log_error_code = log_error_code
                row.error_code = error_code
                row.cleanup_error_code = cleanup_error_code
                row.result_artifacts = [
                    SkillExecutionArtifact(
                        file_id=item.id,
                        filename=item.filename,
                        label=item.label,
                        mime_type=item.mime_type,
                        size_bytes=item.size_bytes,
                        sha256=item.sha256,
                        object_key=item.object_key,
                        output_index=item.output_index,
                    ).model_dump(mode="json")
                    for item in artifacts
                ]
                row.finished_at = finished_at
                await session.flush()
                return skill_execution_to_domain(row)

    async def get_script_execution(
        self, tool_call_record_id: str
    ) -> SkillExecutionRecord | None:
        """读取与现有工具调用一对一的附加记录。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(SkillExecutionModel).where(
                    SkillExecutionModel.tool_call_record_id == tool_call_record_id
                )
            )
        return None if row is None else skill_execution_to_domain(row)

    async def get_admin_execution(self, call_id: str) -> SkillExecutionRecord | None:
        """读取管理员排查所需的脚本元数据和日志对象引用。"""
        return await self.get_script_execution(call_id)

    async def _read_import_result(
        self,
        actor: AuthContext,
        *,
        operation: str,
        target_skill_id: str | None,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> SkillImportRequestResult | None:
        """唯一约束冲突后在新事务中读取已提交的胜者。"""
        async with self._session_factory() as session:
            return await self._read_import_result_in_session(
                session,
                actor,
                operation=operation,
                target_skill_id=target_skill_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )

    @staticmethod
    async def _read_import_result_in_session(
        session: AsyncSession,
        actor: AuthContext,
        *,
        operation: str,
        target_skill_id: str | None,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> SkillImportRequestResult | None:
        """校验请求键后，为个人请求返回当前指针对应的自洽详情。"""
        row = await session.scalar(
            select(SkillVersionModel).where(
                SkillVersionModel.created_by == actor.user.id,
                SkillVersionModel.request_operation == operation,
                SkillVersionModel.idempotency_key == idempotency_key,
            )
        )
        if row is None:
            return None
        if (
            row.request_fingerprint != request_fingerprint
            or row.request_target_skill_id != target_skill_id
        ):
            raise SkillConflictError("idempotency_conflict")
        skill_row = await session.get(SkillModel, row.skill_id)
        if skill_row is None:
            raise SkillConflictError("skill_catalog_integrity_invalid")
        request_version = skill_version_to_domain(row)
        result_version_row = row
        if (
            row.request_operation in _PERSONAL_REQUEST_OPERATIONS
            and skill_row.deleted_at is None
        ):
            if skill_row.current_version_id is None:
                raise SkillConflictError("skill_catalog_integrity_invalid")
            current_version_row = await session.get(
                SkillVersionModel,
                skill_row.current_version_id,
            )
            if (
                current_version_row is None
                or current_version_row.skill_id != skill_row.id
            ):
                raise SkillConflictError("skill_catalog_integrity_invalid")
            result_version_row = current_version_row
        grants_map = await load_grants_map_in_session(session, ResourceType.SKILL, (skill_row.id,))
        return SkillImportRequestResult(
            skill=skill_to_domain(
                skill_row,
                result_version_row,
                grants=grants_map.get(skill_row.id, ()),
            ),
            version=skill_version_to_domain(result_version_row),
            request_version=request_version,
        )

    @staticmethod
    async def _require_skill_manager_session(session: AsyncSession, actor: AuthContext) -> UserRole:
        """在技能管理写事务内重新锁定并校验当前超管或部门管理员会话仍然有效。"""
        guard = SqlAlchemyAuthTransactionGuard(session)
        user = await guard.lock_current_session_user(actor.user.id, actor.session_id)
        if user is None or user.role not in {UserRole.SUPER_ADMIN, UserRole.DEPT_ADMIN}:
            raise PermissionError("admin_required")
        return user.role

    @staticmethod
    async def _require_skill_manager(
        session: AsyncSession,
        actor: AuthContext,
        *,
        scope: SkillScope,
        owner_department_id: str | None,
    ) -> None:
        """在事务内同步复核当前操作者对目标公司/部门 Skill 的实时管理权限，消除 TOCTOU。"""
        role = await SqlAlchemySkillRepository._require_skill_manager_session(session, actor)
        if role is UserRole.SUPER_ADMIN:
            return
        snapshot = await load_user_access_snapshot_in_session(session, actor.user.id)
        if not evaluate_can_manage(
            snapshot,
            ResourceType.SKILL,
            VisibilityScope(scope.value),
            owner_department_id=owner_department_id,
        ):
            raise PermissionError("admin_required")

    @staticmethod
    async def _require_active_user(session: AsyncSession, actor: AuthContext) -> None:
        """在个人写事务内复核当前账号及登录会话仍然有效。"""
        guard = SqlAlchemyAuthTransactionGuard(session)
        user = await guard.lock_current_session_user(actor.user.id, actor.session_id)
        if user is None:
            raise PermissionError("not_authenticated")

    @staticmethod
    def _version_model(
        *,
        version_id: str,
        skill_id: str,
        ordinal: int,
        label: str,
        manifest: SkillPackageManifest,
        object_key: str,
        package_sha256: str,
        dependency_check: DependencyCheck,
        created_by: str,
        request_operation: str,
        idempotency_key: str,
        request_fingerprint: str,
        request_target_skill_id: str | None,
        created_at: datetime,
    ) -> SkillVersionModel:
        """构造不可变版本行，集中避免漏存显式清单字段。"""
        return SkillVersionModel(
            id=version_id,
            skill_id=skill_id,
            ordinal=ordinal,
            label=label,
            guide=manifest.guide,
            manifest=manifest.model_dump(mode="json"),
            object_key=object_key,
            package_sha256=package_sha256,
            dependency_requirements=list(manifest.dependencies),
            dependency_check=dependency_check.model_dump(mode="json"),
            created_by=created_by,
            request_operation=request_operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            request_target_skill_id=request_target_skill_id,
            created_at=created_at,
        )


__all__ = ["SqlAlchemySkillRepository", "build_run_skill_snapshot"]
