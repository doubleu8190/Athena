"""Skill 包提交响应丢失与延迟可见性的对象保留回归。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from access_support import make_test_access_policy

from app.skill.model import SkillAdminDetails
from app.skill.service import SkillAuthorizationError, SkillService
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.skill import (
    DependencyCheck,
    DependencyCheckStatus,
    PackageResource,
    Skill,
    SkillConflictError,
    SkillImportRequestResult,
    SkillPackageManifest,
    SkillVersion,
)

NOW = datetime(2026, 11, 12, tzinfo=UTC)


class _Objects:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def put_file(self, key: str, path: Path, *, content_type: str) -> None:
        del key, path, content_type

    async def delete(self, key: str) -> None:
        self.deleted.append(key)

    async def aclose(self) -> None:
        return None


class _Auth:
    def __init__(self, user: User) -> None:
        self.user = user

    async def get_current_session_user(
        self, user_id: str, session_id: str, now: datetime
    ) -> User | None:
        del session_id, now
        return self.user if self.user.id == user_id else None


class _Repository:
    def __init__(
        self,
        *,
        skill: Skill,
        version: SkillVersion,
        create_error: Exception | None = None,
        commit_before_error: bool = False,
        query_unavailable: bool = False,
        find_permission_on_call: int | None = None,
        get_skill_permission: bool = False,
    ) -> None:
        self.skill = skill
        self.version = version
        self.create_error = create_error
        self.commit_before_error = commit_before_error
        self.query_unavailable = query_unavailable
        self.find_permission_on_call = find_permission_on_call
        self.get_skill_permission = get_skill_permission
        self.replay: SkillImportRequestResult | None = None
        self.find_calls = 0

    async def find_import_result(
        self,
        actor: AuthContext,
        **kwargs: object,
    ) -> SkillImportRequestResult | None:
        del actor, kwargs
        self.find_calls += 1
        if self.find_permission_on_call == self.find_calls:
            raise PermissionError("admin revoked during idempotency read")
        if self.query_unavailable and self.find_calls >= 2:
            raise RuntimeError("database read temporarily unavailable")
        # 模拟提交事务在响应异常后的下一调度周期才对外可见。
        if self.commit_before_error and self.find_calls >= 2:
            self.replay = SkillImportRequestResult(self.skill, self.version)
            return self.replay
        return self.replay

    async def get_skill(self, skill_id: str) -> Skill | None:
        if self.get_skill_permission:
            raise PermissionError("admin revoked during target read")
        return self.skill if skill_id == self.skill.id else None

    async def create_skill(self, **kwargs: object) -> tuple[Skill, SkillVersion]:
        del kwargs
        if self.create_error is not None:
            if self.commit_before_error:
                self.replay = SkillImportRequestResult(self.skill, self.version)
            raise self.create_error
        return self.skill, self.version

    async def add_version(self, skill_id: str, **kwargs: object) -> SkillVersion | None:
        del skill_id, kwargs
        if self.create_error is not None:
            if self.commit_before_error:
                self.replay = SkillImportRequestResult(self.skill, self.version)
            raise self.create_error
        return self.version


def _domain_records() -> tuple[Skill, SkillVersion]:
    manifest = SkillPackageManifest(
        slug="monthly-report",
        name="monthly report",
        description="report",
        guide="guide",
        files=(
            PackageResource(
                path="SKILL.md",
                size_bytes=10,
                sha256="a" * 64,
                text=True,
            ),
        ),
    )
    check = DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version="3.14.0",
        python_implementation="CPython",
        checked_at=NOW,
    )
    version = SkillVersion(
        id="skillver_one",
        skill_id="skill_one",
        ordinal=1,
        label="v1",
        manifest=manifest,
        object_key=f"skills/aa/{'a' * 64}/skillver_one.zip",
        package_sha256="a" * 64,
        dependency_requirements=(),
        dependency_check=check,
        created_by="usr_admin",
        created_at=NOW,
    )
    skill = Skill(
        id="skill_one",
        slug=manifest.slug,
        current_version_id=None,
        enabled=False,
        script_requires_approval=False,
        revision=1,
        created_by="usr_admin",
        created_at=NOW,
        updated_at=NOW,
        name=manifest.name,
        description=manifest.description,
        current_version_label="v1",
        dependency_check=check,
    )
    return skill, version


def _context() -> AuthContext:
    user = User(
        id="usr_admin",
        email="admin@example.test",
        password_hash="hash",
        display_name=None,
        status=UserStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        role=UserRole.SUPER_ADMIN,
    )
    return AuthContext(user, "auth_admin")


def _service(repository: Any, objects: _Objects) -> SkillService:
    _, version = _domain_records()
    package = SimpleNamespace(
        package_sha256=version.package_sha256,
        manifest=version.manifest,
    )
    validator = SimpleNamespace(validate=lambda path: package)
    checker = SimpleNamespace(check=lambda requirements, *, checked_at: version.dependency_check)
    ids = iter(("skill_one", "skillver_one"))
    return SkillService(
        cast(Any, repository),
        cast(Any, objects),
        _Auth(_context().user),
        package_validator=cast(Any, validator),
        dependency_checker=cast(Any, checker),
        id_factory=lambda prefix: next(ids),
        clock=lambda: NOW,
        access_policy=make_test_access_policy(),
    )


@pytest.mark.parametrize(
    ("operation", "error"),
    [
        ("import", RuntimeError("postgresql://secret/internal")),
        ("import", asyncio.CancelledError()),
        ("add_version", RuntimeError("postgresql://secret/internal")),
        ("add_version", asyncio.CancelledError()),
    ],
)
def test_unknown_or_cancelled_package_commit_retains_object(
    tmp_path: Path,
    operation: str,
    error: Exception,
) -> None:
    """查询暂时未命中不能证明提交不会稍后完成。"""
    skill, version = _domain_records()
    repository = _Repository(skill=skill, version=version, create_error=error)
    repository.replay = None
    objects = _Objects()
    service = _service(repository, objects)
    actor = _context()

    async def scenario() -> None:
        with pytest.raises(type(error)):
            if operation == "import":
                await service.import_package(
                    actor,
                    tmp_path / "package.zip",
                    idempotency_key="import-key",
                )
            else:
                await service.add_version(
                    actor,
                    skill.id,
                    tmp_path / "package.zip",
                    idempotency_key="version-key",
                )

    asyncio.run(scenario())
    assert objects.deleted == []
    if isinstance(error, asyncio.CancelledError):
        assert repository.find_calls == 1  # 只执行提交前的预读


@pytest.mark.parametrize("operation", ["import", "add_version"])
def test_committed_package_response_failure_replays_without_deleting(
    tmp_path: Path,
    operation: str,
) -> None:
    """事务已提交但返回异常时，幂等复核必须返回可见结果。"""
    skill, version = _domain_records()
    repository = _Repository(
        skill=skill,
        version=version,
        create_error=RuntimeError("response lost"),
        commit_before_error=True,
    )
    objects = _Objects()
    service = _service(repository, objects)

    async def scenario() -> SkillAdminDetails | SkillVersion:
        if operation == "import":
            return await service.import_package(
                _context(),
                tmp_path / "package.zip",
                idempotency_key="import-key",
            )
        return await service.add_version(
            _context(),
            skill.id,
            tmp_path / "package.zip",
            idempotency_key="version-key",
        )

    result = asyncio.run(scenario())
    assert objects.deleted == []
    if operation == "import":
        assert isinstance(result, SkillAdminDetails)
        assert result.skill.id == skill.id
    else:
        assert isinstance(result, SkillVersion)
        assert result.id == version.id


@pytest.mark.parametrize("operation", ["import", "add_version"])
def test_eventually_visible_package_commit_is_not_deleted_by_early_miss(
    tmp_path: Path,
    operation: str,
) -> None:
    """异常后的即时复核未命中时，稍后才可见的版本提交仍须保有对象。"""

    class DelayedRepository(_Repository):
        def __init__(self, skill: Skill, version: SkillVersion) -> None:
            super().__init__(
                skill=skill,
                version=version,
                create_error=RuntimeError("commit outcome unknown"),
            )
            self.commit_visible = False
            self.pending_import: dict[str, object] | None = None
            self.pending_version: tuple[str, dict[str, object]] | None = None

        async def find_import_result(
            self,
            actor: AuthContext,
            **kwargs: object,
        ) -> SkillImportRequestResult | None:
            if not self.commit_visible:
                self.find_calls += 1
                return None
            return await super().find_import_result(actor, **kwargs)

        async def create_skill(
            self,
            **kwargs: object,
        ) -> tuple[Skill, SkillVersion]:
            self.pending_import = kwargs
            raise RuntimeError("commit outcome unknown")

        async def add_version(
            self,
            skill_id: str,
            **kwargs: object,
        ) -> SkillVersion | None:
            self.pending_version = (skill_id, kwargs)
            raise RuntimeError("commit outcome unknown")

        async def finish_delayed_commit(self) -> None:
            self.create_error = None
            if self.pending_import is not None:
                await super().create_skill(**self.pending_import)
            else:
                assert self.pending_version is not None
                skill_id, kwargs = self.pending_version
                await super().add_version(skill_id, **kwargs)
            self.replay = SkillImportRequestResult(self.skill, self.version)
            self.commit_visible = True

    skill, version = _domain_records()
    repository = DelayedRepository(skill, version)
    objects = _Objects()
    service = _service(repository, objects)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="commit outcome unknown"):
            if operation == "import":
                await service.import_package(
                    _context(),
                    tmp_path / "package.zip",
                    idempotency_key="import-delayed",
                )
            else:
                await service.add_version(
                    _context(),
                    skill.id,
                    tmp_path / "package.zip",
                    idempotency_key="version-delayed",
                )
        assert repository.find_calls == 2
        # 即时复核为空时对象仍原样保留，后续可见提交可以直接接管该引用。
        assert objects.deleted == []
        await repository.finish_delayed_commit()

    asyncio.run(scenario())
    assert objects.deleted == []


def test_explicit_skill_conflict_deletes_only_after_rolled_back_transaction(
    tmp_path: Path,
) -> None:
    """唯一约束业务冲突已明确回滚，失败请求对象可立即清理。"""
    skill, version = _domain_records()
    repository = _Repository(
        skill=skill,
        version=version,
        create_error=SkillConflictError("skill_slug_exists"),
    )
    objects = _Objects()
    service = _service(repository, objects)

    async def scenario() -> None:
        with pytest.raises(SkillConflictError):
            await service.import_package(
                _context(),
                tmp_path / "package.zip",
                idempotency_key="import-key",
            )

    asyncio.run(scenario())
    assert len(objects.deleted) == 1


def test_package_failure_with_unavailable_replay_query_retains_object(
    tmp_path: Path,
) -> None:
    """提交异常后的幂等查询若暂不可用，对象必须保留到停写维护。"""
    skill, version = _domain_records()
    repository = _Repository(
        skill=skill,
        version=version,
        create_error=RuntimeError("commit outcome unknown"),
        query_unavailable=True,
    )
    objects = _Objects()
    service = _service(repository, objects)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="commit outcome unknown"):
            await service.import_package(
                _context(),
                tmp_path / "package.zip",
                idempotency_key="import-query-down",
            )

    asyncio.run(scenario())
    assert repository.find_calls == 2
    assert objects.deleted == []


@pytest.mark.parametrize("operation", ["import", "add_version"])
def test_replay_after_failure_permission_error_maps_to_authorization(
    tmp_path: Path,
    operation: str,
) -> None:
    """提交结果未知后的幂等回查撤权也必须返回 403，并保留对象。"""
    skill, version = _domain_records()
    repository = _Repository(
        skill=skill,
        version=version,
        create_error=RuntimeError("commit outcome unknown"),
        find_permission_on_call=2,
    )
    objects = _Objects()
    service = _service(repository, objects)

    async def scenario() -> None:
        with pytest.raises(SkillAuthorizationError, match="admin_required"):
            if operation == "import":
                await service.import_package(
                    _context(),
                    tmp_path / "package.zip",
                    idempotency_key="import-replay-revoked",
                )
            else:
                await service.add_version(
                    _context(),
                    skill.id,
                    tmp_path / "package.zip",
                    idempotency_key="version-replay-revoked",
                )

    asyncio.run(scenario())
    assert repository.find_calls == 2
    assert objects.deleted == []


@pytest.mark.parametrize("operation", ["import", "add_version"])
def test_transaction_permission_error_maps_to_authorization_and_retains_object(
    tmp_path: Path,
    operation: str,
) -> None:
    """事务内授权失效统一返回 403 语义，且不能清理状态不明的包对象。"""
    skill, version = _domain_records()
    repository = _Repository(
        skill=skill,
        version=version,
        create_error=PermissionError("transaction permission denied"),
    )
    objects = _Objects()
    service = _service(repository, objects)

    async def scenario() -> None:
        with pytest.raises(SkillAuthorizationError, match="admin_required"):
            if operation == "import":
                await service.import_package(
                    _context(),
                    tmp_path / "package.zip",
                    idempotency_key="import-permission",
                )
            else:
                await service.add_version(
                    _context(),
                    skill.id,
                    tmp_path / "package.zip",
                    idempotency_key="version-permission",
                )

    asyncio.run(scenario())
    assert repository.find_calls == 1
    assert objects.deleted == []


@pytest.mark.parametrize("operation", ["import", "add_version"])
def test_initial_idempotency_read_permission_error_maps_to_authorization(
    tmp_path: Path,
    operation: str,
) -> None:
    """提交前幂等重查撤权也必须稳定返回管理员 403，而不是 500。"""
    skill, version = _domain_records()
    repository = _Repository(
        skill=skill,
        version=version,
        find_permission_on_call=1,
    )
    objects = _Objects()
    service = _service(repository, objects)

    async def scenario() -> None:
        with pytest.raises(SkillAuthorizationError, match="admin_required"):
            if operation == "import":
                await service.import_package(
                    _context(),
                    tmp_path / "package.zip",
                    idempotency_key="import-read-revoked",
                )
            else:
                await service.add_version(
                    _context(),
                    skill.id,
                    tmp_path / "package.zip",
                    idempotency_key="version-read-revoked",
                )

    asyncio.run(scenario())
    assert repository.find_calls == 1
    assert objects.deleted == []


def test_add_version_initial_target_read_permission_error_maps_to_authorization(
    tmp_path: Path,
) -> None:
    """add_version 读取目标 Skill 时撤权同样映射为管理员 403。"""
    skill, version = _domain_records()
    repository = _Repository(
        skill=skill,
        version=version,
        get_skill_permission=True,
    )
    objects = _Objects()
    service = _service(repository, objects)

    async def scenario() -> None:
        with pytest.raises(SkillAuthorizationError, match="admin_required"):
            await service.add_version(
                _context(),
                skill.id,
                tmp_path / "package.zip",
                idempotency_key="version-target-revoked",
            )

    asyncio.run(scenario())
    assert repository.find_calls == 0
    assert objects.deleted == []
