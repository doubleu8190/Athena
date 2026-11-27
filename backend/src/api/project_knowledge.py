"""项目知识库关联 HTTP 接口。"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from api.converter.knowledge import knowledge_base_to_response
from api.dto.knowledge import KnowledgeBaseResponse
from api.errors import DomainErrorRoute
from api.security import CurrentAuth
from app.project.knowledge import ProjectKnowledgeService
from app.project.service import ProjectService


def build_project_knowledge_router(
    service: ProjectKnowledgeService, projects: ProjectService
) -> APIRouter:
    """创建项目知识库关联路由。"""
    del projects
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/knowledge-bases",
        tags=["project-knowledge"],
        route_class=DomainErrorRoute,
    )

    @router.get("", response_model=list[KnowledgeBaseResponse])
    async def list_bases(project_id: str, auth: CurrentAuth) -> list[KnowledgeBaseResponse]:
        bases = await service.list(auth.user.id, project_id)
        return [knowledge_base_to_response(base) for base in bases]

    @router.put(
        "/{base_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def bind(project_id: str, base_id: str, auth: CurrentAuth) -> Response:
        await service.bind(auth.user.id, project_id, base_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.delete(
        "/{base_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def unbind(project_id: str, base_id: str, auth: CurrentAuth) -> Response:
        await service.unbind(auth.user.id, project_id, base_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
