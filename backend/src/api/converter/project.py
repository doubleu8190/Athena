"""将项目应用结果转换为 HTTP DTO。"""

from __future__ import annotations

from api.dto.project import ProjectListResponse, ProjectResponse
from app.project.model.result import ProjectPageResult, ProjectResult


def project_to_response(project: ProjectResult) -> ProjectResponse:
    """转换单个项目响应。"""
    return ProjectResponse(
        id=project.id,
        name=project.name,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def project_list_to_response(
    page: ProjectPageResult,
    *,
    page_number: int,
    page_size: int,
) -> ProjectListResponse:
    """转换项目分页响应。"""
    return ProjectListResponse(
        items=[project_to_response(project) for project in page.items],
        total=page.total,
        page=page_number,
        page_size=page_size,
    )


__all__ = ["project_list_to_response", "project_to_response"]
