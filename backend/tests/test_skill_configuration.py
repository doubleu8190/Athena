"""Skill 停用探测与事务授权映射回归。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from app.skill.service import SkillAuthorizationError, SkillService
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.skill import (
    DependencyCheck,
    DependencyCheckStatus,
    PackageResource,
    Skill,
    SkillPackageManifest,
    SkillVersion,
)
from domain.skill.model import DependencyCheckItem

NOW = datetime(2026, 11, 12, tzinfo=UTC)


def _records() -> tuple[Skill, SkillVersion, DependencyCheck]:
    check = DependencyCheck(
        status=DependencyCheckStatus.UNSATISFIED,
        python_version="3.14.0",
        python_implementation="CPython",
        items=(
            DependencyCheckItem(
                requirement="demo-package>=2",
                package="demo-package",
                satisfied=False,
                installed_version="1.0",
                detail_code="dependency_version_mismatch",
            ),
        ),
        checked_at=NOW,
    )
    manifest = SkillPackageManifest(
        slug="monthly-report",
        name="monthly report",
        description="report",
        guide="guide",
        files=(
            PackageResource(
                path="SKILL.md",
                size_bytes=5,
                sha256="a" * 64,
                text=True,
            ),
        ),
    )
    version = SkillVersion(
        id="skillver_one",
        skill_id="skill_one",
        ordinal=1,
        label="v1",
        manifest=manifest,
        object_key=f"skills/aa/{'a' * 64}/skillver_one.zip",
        package_sha256="a" * 64,
        dependency_requirements=("demo-package>=2",),
        dependency_check=check,
        created_by="usr_admin",
        created_at=NOW,
    )
    skill = Skill(
        id="skill_one",
        slug=manifest.slug,
        current_version_id=version.id,
        enabled=True,
        script_requires_approval=False,
        revision=1,
        created_by="usr_admin",
        created_at=NOW,
        updated_at=NOW,
        name=manifest.name,
        description=manifest.description,
        current_version_label=version.label,
        dependency_check=check,
    )
    return skill, version, check


def _actor() -> AuthContext:
    user = User(
        id="usr_admin",
        email="admin@example.test",
        password_hash="hash",
        display_name=None,
        status=UserStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        role=UserRole.ADMIN,
    )
    return AuthContext(user, "auth_admin")


class _Auth:
    def __init__(self, actor: AuthContext) -> None:
        self.actor = actor

    async def get_current_session_user(
        self,
        user_id: str,
        session_id: str,
        now: datetime,
    ) -> User | None:
        del session_id, now
        return self.actor.user if user_id == self.actor.user.id else None


class _Checker:
    def __init__(self) -> None:
        self.calls = 0

    def check(
        self,
        requirements: tuple[str, ...],
        *,
        checked_at: datetime,
    ) -> DependencyCheck:
        del requirements, checked_at
        self.calls += 1
        raise AssertionError("停用不应执行 fresh dependency probe")


class _Repository:
    def __init__(
        self,
        *,
        skill: Skill,
        version: SkillVersion,
        error: Exception | None = None,
    ) -> None:
        self.skill = skill
        self.version = version
        self.error = error
        self.configuration: dict[str, object] | None = None

    async def get_version(self, version_id: str) -> SkillVersion | None:
        return self.version if version_id == self.version.id else None

    async def update_configuration(
        self,
        skill_id: str,
        **kwargs: object,
    ) -> Skill:
        del skill_id
        self.configuration = kwargs
        if self.error is not None:
            raise self.error
        return self.skill


def _service(repository: _Repository, checker: _Checker) -> tuple[SkillService, AuthContext]:
    actor = _actor()
    return (
        SkillService(
            cast(Any, repository),
            cast(Any, object()),
            _Auth(actor),
            dependency_checker=cast(Any, checker),
            clock=lambda: NOW,
        ),
        actor,
    )


def test_disabled_skill_skips_fresh_probe_and_preserves_stored_check() -> None:
    skill, version, check = _records()
    repository = _Repository(skill=skill, version=version)
    checker = _Checker()
    service, actor = _service(repository, checker)

    result = asyncio.run(
        service.configure(
            actor,
            skill.id,
            expected_revision=skill.revision,
            enabled=False,
            current_version_id=version.id,
            script_requires_approval=False,
        )
    )

    assert result is skill
    assert checker.calls == 0
    assert repository.configuration is not None
    assert repository.configuration["dependency_check"] is check


def test_transaction_permission_error_maps_to_skill_authorization_error() -> None:
    skill, version, _ = _records()
    repository = _Repository(
        skill=skill,
        version=version,
        error=PermissionError("admin_required"),
    )
    checker = _Checker()
    service, actor = _service(repository, checker)

    with pytest.raises(SkillAuthorizationError, match="admin_required"):
        asyncio.run(
            service.configure(
                actor,
                skill.id,
                expected_revision=skill.revision,
                enabled=False,
                current_version_id=version.id,
                script_requires_approval=False,
            )
        )
