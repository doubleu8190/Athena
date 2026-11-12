"""Skill 领域对象到安全 HTTP DTO 的纯转换。"""

from __future__ import annotations

from api.dto.skill import (
    SkillAdminDetailsResponse,
    SkillAdminListResponse,
    SkillDependencyCheckResponse,
    SkillDependencyItemResponse,
    SkillDirectoryResponse,
    SkillExecutionResponse,
    SkillPackageResourceResponse,
    SkillScriptResponse,
    SkillSummaryResponse,
    SkillVersionListResponse,
    SkillVersionResponse,
)
from app.skill.model import SkillAdminDetails
from domain.skill import DependencyCheck, Skill, SkillExecutionRecord, SkillVersion


def dependency_check_to_response(check: DependencyCheck) -> SkillDependencyCheckResponse:
    """转换依赖检查及目标解释器指纹。"""
    return SkillDependencyCheckResponse(
        status=check.status.value,
        python_version=check.python_version,
        python_implementation=check.python_implementation,
        checked_at=check.checked_at,
        items=[
            SkillDependencyItemResponse(
                requirement=item.requirement,
                package=item.package,
                satisfied=item.satisfied,
                installed_version=item.installed_version,
                detail_code=item.detail_code,
                marker_applies=item.marker_applies,
            )
            for item in check.items
        ],
    )


def skill_to_response(skill: Skill) -> SkillSummaryResponse:
    """转换公开或列表所需的 Skill 摘要。"""
    return SkillSummaryResponse(
        id=skill.id,
        slug=skill.slug,
        name=skill.name,
        description=skill.description,
        current_version_id=skill.current_version_id,
        current_version_label=skill.current_version_label,
        enabled=skill.enabled,
        script_requires_approval=skill.script_requires_approval,
        revision=skill.revision,
        updated_at=skill.updated_at,
        dependency_check=(
            None
            if skill.dependency_check is None
            else dependency_check_to_response(skill.dependency_check)
        ),
    )


def version_to_response(version: SkillVersion) -> SkillVersionResponse:
    """转换管理员版本正文、资源清单和依赖检查。"""
    return SkillVersionResponse(
        id=version.id,
        skill_id=version.skill_id,
        ordinal=version.ordinal,
        label=version.label,
        slug=version.manifest.slug,
        name=version.manifest.name,
        description=version.manifest.description,
        guide=version.manifest.guide,
        dependencies=list(version.dependency_requirements),
        dependency_check=dependency_check_to_response(version.dependency_check),
        scripts=[
            SkillScriptResponse(
                path=item.path,
                label=item.label,
                purpose=item.purpose,
                parameters=item.parameters,
            )
            for item in version.manifest.scripts
        ],
        files=[
            SkillPackageResourceResponse(
                path=item.path,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                text=item.text,
            )
            for item in version.manifest.files
        ],
        package_sha256=version.package_sha256,
        created_at=version.created_at,
    )


def admin_details_to_response(details: SkillAdminDetails) -> SkillAdminDetailsResponse:
    """转换管理员详情并填充当前版本标签。"""
    versions = [version_to_response(item) for item in details.versions]
    current_label = next(
        (
            item.label
            for item in details.versions
            if item.id == details.skill.current_version_id
        ),
        None,
    )
    summary = skill_to_response(details.skill).model_copy(
        update={"current_version_label": current_label}
    )
    return SkillAdminDetailsResponse(
        **summary.model_dump(),
        versions=versions,
    )


def admin_list_to_response(items: tuple[Skill, ...]) -> SkillAdminListResponse:
    """转换管理员列表。"""
    return SkillAdminListResponse(items=[skill_to_response(item) for item in items])


def directory_to_response(items: tuple[Skill, ...]) -> SkillDirectoryResponse:
    """转换普通用户可用目录，不含包正文、对象键或脚本宿主路径。"""
    return SkillDirectoryResponse(items=[skill_to_response(item) for item in items])


def version_page_to_response(
    items: tuple[SkillVersion, ...], *, total: int, page: int, page_size: int
) -> SkillVersionListResponse:
    """转换历史版本分页。"""
    return SkillVersionListResponse(
        items=[version_to_response(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


def execution_to_response(execution: SkillExecutionRecord) -> SkillExecutionResponse:
    """转换管理员执行排查摘要，不返回日志正文或对象键。"""
    return SkillExecutionResponse(
        id=execution.id,
        run_id=execution.run_id,
        session_id=execution.session_id,
        call_id=execution.tool_call_record_id,
        skill_id=execution.skill_id,
        skill_version_id=execution.skill_version_id,
        script_path=execution.script_path,
        input_file_ids=list(execution.input_file_ids),
        python_version=execution.python_version,
        dependency_check=dependency_check_to_response(execution.dependency_check),
        status=execution.status.value,
        log_available=execution.log_object_key is not None,
        log_truncated=execution.log_truncated,
        log_error_code=execution.log_error_code,
        error_code=execution.error_code,
        cleanup_error_code=execution.cleanup_error_code,
        created_at=execution.created_at,
        finished_at=execution.finished_at,
    )


__all__ = [
    "admin_details_to_response",
    "admin_list_to_response",
    "dependency_check_to_response",
    "directory_to_response",
    "execution_to_response",
    "skill_to_response",
    "version_page_to_response",
    "version_to_response",
]
