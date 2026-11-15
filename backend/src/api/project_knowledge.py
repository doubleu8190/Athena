"""项目知识库关联 HTTP 接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from api.converter.knowledge import knowledge_base_to_response
from api.dto.knowledge import KnowledgeBaseResponse
from api.security import CurrentAuth, verify_request_origin
from app.project.knowledge import KnowledgeNotFoundError, ProjectKnowledgeService
from app.project.service import ProjectService
from domain.knowledge.model import ProjectKnowledgeLimitReachedError
from domain.project import ProjectNotFoundError


def build_project_knowledge_router(
    service: ProjectKnowledgeService, projects: ProjectService
) -> APIRouter:
    """创建项目知识库关联路由。"""
    router = APIRouter(
        prefix="/api/v1/projects/{project_id}/knowledge-bases",
        tags=["project-knowledge"],
        dependencies=[Depends(verify_request_origin)],
    )

    @router.get("", response_model=list[KnowledgeBaseResponse])
    async def list_bases(project_id: str, auth: CurrentAuth) -> list[KnowledgeBaseResponse]:
        try:
            bases = await service.list(auth.user.id, project_id)
        except ProjectNotFoundError as error:
            raise HTTPException(status_code=404, detail="project_not_found") from error
        return [knowledge_base_to_response(base) for base in bases]

    @router.put("/{base_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def bind(project_id: str, base_id: str, auth: CurrentAuth) -> Response:
        try:
            await service.bind(auth.user.id, project_id, base_id)
        except ProjectNotFoundError as error:
            raise HTTPException(status_code=404, detail="project_not_found") from error
        except KnowledgeNotFoundError as error:
            raise HTTPException(status_code=404, detail="knowledge_base_not_found") from error
        except ProjectKnowledgeLimitReachedError as error:
            raise HTTPException(
                status_code=409,
                detail="project_knowledge_limit_reached",
            ) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.delete("/{base_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def unbind(project_id: str, base_id: str, auth: CurrentAuth) -> Response:
        try:
            await service.unbind(auth.user.id, project_id, base_id)
        except ProjectNotFoundError as error:
            raise HTTPException(status_code=404, detail="project_not_found") from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
