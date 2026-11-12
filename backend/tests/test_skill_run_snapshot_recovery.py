"""Run Skill 快照不得把旧依赖检查当作永久门禁。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

from adapter.skill.model import SkillModel, SkillVersionModel
from adapter.skill.repository import build_run_skill_snapshot
from domain.skill import (
    DependencyCheck,
    DependencyCheckStatus,
    PackageResource,
    SkillPackageManifest,
)
from domain.skill.model import DependencyCheckItem

NOW = datetime(2026, 11, 12, tzinfo=UTC)


class _Rows:
    def __init__(self, rows: list[tuple[SkillModel, SkillVersionModel]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[SkillModel, SkillVersionModel]]:
        return self._rows


class _Session:
    def __init__(self, rows: list[tuple[SkillModel, SkillVersionModel]]) -> None:
        self._rows = rows

    async def execute(self, _statement: object) -> _Rows:
        return _Rows(self._rows)


def _catalog_row(
    *,
    enabled: bool = True,
) -> tuple[SkillModel, SkillVersionModel]:
    manifest = SkillPackageManifest(
        slug="recovered-skill",
        name="Recovered Skill",
        description="依赖恢复后可直接进入新 Run 快照。",
        guide="执行前会再次检查真实依赖。",
        files=(
            PackageResource(
                path="SKILL.md",
                size_bytes=10,
                sha256="a" * 64,
                text=True,
            ),
        ),
    )
    old_check = DependencyCheck(
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
    skill = SkillModel(
        id="skill_recovery",
        slug=manifest.slug,
        current_version_id="skillver_recovery",
        enabled=enabled,
        script_requires_approval=False,
        revision=1,
        created_by=None,
        created_at=NOW,
        updated_at=NOW,
    )
    version = SkillVersionModel(
        id="skillver_recovery",
        skill_id=skill.id,
        ordinal=1,
        label="v1",
        guide=manifest.guide,
        manifest=manifest.model_dump(mode="json"),
        object_key="skills/aa/recovery.zip",
        package_sha256="b" * 64,
        dependency_requirements=["demo-package>=2"],
        dependency_check=old_check.model_dump(mode="json"),
        created_by=None,
        request_operation="import",
        idempotency_key="import-recovery",
        request_fingerprint="c" * 64,
        request_target_skill_id=None,
        created_at=NOW,
    )
    return skill, version


def test_old_unsatisfied_check_does_not_hide_recovered_skill_from_run_snapshot() -> None:
    """依赖已恢复时，新 Run 直接提交不能被持久化的旧 unsatisfied 永久阻断。"""
    skill, version = _catalog_row()

    snapshot = asyncio.run(
        build_run_skill_snapshot(
            cast(Any, _Session([(skill, version)])),
            (skill.id,),
        )
    )

    assert snapshot.selected_skill_ids == (skill.id,)
    assert len(snapshot.skills) == 1
    assert snapshot.skills[0].version_id == version.id
