"""将项目领域模型转换为 HTTP DTO。"""

from __future__ import annotations

from api.dto.project import ProjectListResponse, ProjectResponse
from domain.project import Project, ProjectPage


def project_to_response(project: Project) -> ProjectResponse:
    """按 DTO 字段白名单投影单个项目响应，自动屏蔽 owner_id 与 deleted_time 等内部属性。"""
    return ProjectResponse.model_validate(project)


def project_list_to_response(
    page: ProjectPage,
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
