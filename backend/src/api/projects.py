"""项目目录 HTTP 接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status

from api.converter.project import project_list_to_response, project_to_response
from api.dto.project import (
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectResponse,
    ProjectUpdateRequest,
)
from api.security import CurrentAuth, verify_request_origin
from app.project import (
    InvalidProjectInputError,
    InvalidProjectNameError,
    ProjectHasSessionsError,
    ProjectNotFoundError,
    ProjectService,
)
from domain.run.model.errors import SessionHasActiveRunError

PROJECT_NOT_FOUND = "project_not_found"
PROJECT_NAME_INVALID = "project_name_invalid"
PROJECT_HAS_SESSIONS = "project_has_sessions"
SESSION_HAS_ACTIVE_RUN = "session_has_active_run"


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
        request: ProjectCreateRequest,
        response: Response,
        current_auth: CurrentAuth,
    ) -> ProjectResponse:
        """创建项目。"""
        try:
            project = await service.create(
                current_auth.user.id,
                request.name,
                description=request.description,
                instructions=request.instructions,
                default_skill_ids=request.default_skill_ids,
            )
        except InvalidProjectNameError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=PROJECT_NAME_INVALID,
            ) from error
        except InvalidProjectInputError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
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
    async def update_project(
        current_auth: CurrentAuth,
        project_id: Annotated[str, Path(min_length=1, max_length=64)],
        request: ProjectUpdateRequest,
    ) -> ProjectResponse:
        """局部更新未删除项目的名称、简介、背景指令或默认技能。"""
        supplied_fields = request.model_fields_set
        try:
            project = await service.update(
                current_auth.user.id,
                project_id,
                update_name="name" in supplied_fields,
                name=request.name,
                update_description="description" in supplied_fields,
                description=request.description,
                update_instructions="instructions" in supplied_fields,
                instructions=request.instructions,
                update_default_skills="default_skill_ids" in supplied_fields,
                default_skill_ids=request.default_skill_ids,
            )
        except InvalidProjectNameError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=PROJECT_NAME_INVALID,
            ) from error
        except InvalidProjectInputError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
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
        response_class=Response,
    )
    async def delete_project(
        current_auth: CurrentAuth,
        project_id: Annotated[str, Path(min_length=1, max_length=64)],
        detach_sessions: bool = False,
    ) -> Response:
        """软删除项目；detach_sessions=True 时先解绑无活跃 Run 的关联会话。"""
        try:
            await service.delete(
                current_auth.user.id,
                project_id,
                detach_sessions=detach_sessions,
            )
        except ProjectHasSessionsError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=PROJECT_HAS_SESSIONS,
            ) from error
        except SessionHasActiveRunError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=SESSION_HAS_ACTIVE_RUN,
            ) from error
        except ProjectNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=PROJECT_NOT_FOUND,
            ) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


__all__ = ["build_project_router"]
