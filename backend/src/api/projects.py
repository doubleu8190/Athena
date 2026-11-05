"""项目目录 HTTP 接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status

from api.converter.project import project_list_to_response, project_to_response
from api.dto.project import ProjectListResponse, ProjectNameRequest, ProjectResponse
from api.security import CurrentAuth, verify_request_origin
from app.project import (
    InvalidProjectNameError,
    ProjectHasSessionsError,
    ProjectNotFoundError,
    ProjectService,
)

PROJECT_NOT_FOUND = "project_not_found"
PROJECT_NAME_INVALID = "project_name_invalid"
PROJECT_HAS_SESSIONS = "project_has_sessions"


def build_project_router(service: ProjectService) -> APIRouter:
    """绑定项目用例并创建路由。"""
    router = APIRouter(
        prefix="/api/v1/projects",
        tags=["projects"],
        dependencies=[Depends(verify_request_origin)],
    )

    @router.post(
        "",
        response_model=ProjectResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_project(
        request: ProjectNameRequest,
        response: Response,
        current_auth: CurrentAuth,
    ) -> ProjectResponse:
        """创建项目。"""
        try:
            project = await service.create(current_auth.user.id, request.name)
        except InvalidProjectNameError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=PROJECT_NAME_INVALID,
            ) from error
        response.headers["Location"] = f"/api/v1/projects/{project.id}"
        return project_to_response(project)

    @router.get("", response_model=ProjectListResponse)
    async def list_projects(
        current_auth: CurrentAuth,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> ProjectListResponse:
        """分页查询未删除项目。"""
        result = await service.list(
            current_auth.user.id,
            page=page,
            page_size=page_size,
        )
        return project_list_to_response(
            result,
            page_number=page,
            page_size=page_size,
        )

    @router.get("/{project_id}", response_model=ProjectResponse)
    async def get_project(
        current_auth: CurrentAuth,
        project_id: Annotated[str, Path(min_length=1, max_length=64)],
    ) -> ProjectResponse:
        """查询单个未删除项目。"""
        try:
            project = await service.get(current_auth.user.id, project_id)
        except ProjectNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=PROJECT_NOT_FOUND,
            ) from error
        return project_to_response(project)

    @router.patch("/{project_id}", response_model=ProjectResponse)
    async def rename_project(
        current_auth: CurrentAuth,
        project_id: Annotated[str, Path(min_length=1, max_length=64)],
        request: ProjectNameRequest,
    ) -> ProjectResponse:
        """修改未删除项目名称。"""
        try:
            project = await service.rename(
                current_auth.user.id,
                project_id,
                request.name,
            )
        except InvalidProjectNameError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=PROJECT_NAME_INVALID,
            ) from error
        except ProjectNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=PROJECT_NOT_FOUND,
            ) from error
        return project_to_response(project)

    @router.delete(
        "/{project_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_project(
        current_auth: CurrentAuth,
        project_id: Annotated[str, Path(min_length=1, max_length=64)],
    ) -> Response:
        """软删除项目。"""
        try:
            await service.delete(current_auth.user.id, project_id)
        except ProjectHasSessionsError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=PROJECT_HAS_SESSIONS,
            ) from error
        except ProjectNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=PROJECT_NOT_FOUND,
            ) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


__all__ = ["build_project_router"]
