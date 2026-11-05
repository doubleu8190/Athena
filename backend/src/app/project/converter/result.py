"""将项目领域模型转换为应用结果模型。"""

from __future__ import annotations

from app.project.model.result import ProjectPageResult, ProjectResult
from domain.project import Project, ProjectPage


def project_to_result(project: Project) -> ProjectResult:
    """隐藏领域内部软删除字段并生成应用结果。"""
    return ProjectResult(
        id=project.id,
        name=project.name,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def project_page_to_result(page: ProjectPage) -> ProjectPageResult:
    """批量转换项目分页结果。"""
    return ProjectPageResult(
        items=tuple(project_to_result(project) for project in page.items),
        total=page.total,
    )


__all__ = ["project_page_to_result", "project_to_result"]
