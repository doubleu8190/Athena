"""Skill ORM 行到显式领域对象的纯转换。"""

from __future__ import annotations

from datetime import UTC, datetime

from adapter.skill.model import SkillExecutionModel, SkillModel, SkillVersionModel
from domain.access import VisibilityGrant
from domain.skill.model import (
    DependencyCheck,
    Skill,
    SkillExecutionArtifact,
    SkillExecutionRecord,
    SkillExecutionStatus,
    SkillPackageManifest,
    SkillScope,
    SkillVersion,
)


def _utc(value: datetime) -> datetime:
    """补齐 SQLite 丢失的 UTC 时区。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def skill_version_to_domain(row: SkillVersionModel) -> SkillVersion:
    """转换不可变版本并验证持久化 JSON。"""
    manifest = SkillPackageManifest.model_validate(row.manifest)
    dependency_check = DependencyCheck.model_validate(row.dependency_check)
    return SkillVersion(
        id=row.id,
        skill_id=row.skill_id,
        ordinal=row.ordinal,
        label=row.label,
        manifest=manifest,
        object_key=row.object_key,
        package_sha256=row.package_sha256,
        dependency_requirements=tuple(row.dependency_requirements),
        dependency_check=dependency_check,
        created_by=row.created_by,
        created_at=_utc(row.created_at),
    )


def skill_to_domain(
    row: SkillModel,
    version: SkillVersionModel | None = None,
    *,
    grants: tuple[VisibilityGrant, ...] = (),
) -> Skill:
    """转换 Skill 身份；调用方可附带当前版本及可见性授权列表。"""
    current = None if version is None else skill_version_to_domain(version)
    return Skill(
        id=row.id,
        slug=row.slug,
        current_version_id=row.current_version_id,
        enabled=row.enabled,
        script_requires_approval=row.script_requires_approval,
        revision=row.revision,
        created_by=row.created_by,
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
        name="" if current is None else current.manifest.name,
        description="" if current is None else current.manifest.description,
        current_version_label=None if current is None else current.label,
        dependency_check=None if current is None else current.dependency_check,
        scope=SkillScope(row.scope),
        owner_id=row.owner_id,
        deleted_at=None if row.deleted_at is None else _utc(row.deleted_at),
        owner_department_id=row.owner_department_id,
        visibility_grants=grants,
    )


def skill_execution_to_domain(row: SkillExecutionModel) -> SkillExecutionRecord:
    """转换脚本执行附加记录。"""
    return SkillExecutionRecord(
        id=row.id,
        run_id=row.run_id,
        session_id=row.session_id,
        tool_call_record_id=row.tool_call_record_id,
        skill_id=row.skill_id,
        skill_version_id=row.skill_version_id,
        script_path=row.script_path,
        input_file_ids=tuple(row.input_file_ids),
        parameters_sha256=row.parameters_sha256,
        python_version=row.python_version,
        dependency_check=DependencyCheck.model_validate(row.dependency_check),
        status=SkillExecutionStatus(row.status),
        log_object_key=row.log_object_key,
        log_truncated=row.log_truncated,
        log_error_code=row.log_error_code,
        error_code=row.error_code,
        cleanup_error_code=row.cleanup_error_code,
        result_artifacts=tuple(
            SkillExecutionArtifact.model_validate(item)
            for item in row.result_artifacts
        ),
        created_at=_utc(row.created_at),
        finished_at=None if row.finished_at is None else _utc(row.finished_at),
    )


__all__ = [
    "skill_execution_to_domain",
    "skill_to_domain",
    "skill_version_to_domain",
]
