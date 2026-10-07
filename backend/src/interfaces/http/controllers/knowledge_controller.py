"""知识库和知识文档写入 Controller。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from application.knowledge import (
    KnowledgeBaseNotFoundError,
    KnowledgeBaseService,
    KnowledgeDocumentNotFoundError,
)
from interfaces.http.controllers.files_controller import AttachmentResponse
from interfaces.http.controllers.query_controller import KnowledgeBaseResponse


class KnowledgeBaseCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)


class KnowledgeBaseUpdateRequest(KnowledgeBaseCreateRequest):
    pass


def build_knowledge_router(
    service_factory: Callable[[], KnowledgeBaseService],
) -> APIRouter:
    router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])

    def service() -> KnowledgeBaseService:
        return service_factory()

    @router.get("")
    async def list_knowledge_bases() -> list[KnowledgeBaseResponse]:
        return [KnowledgeBaseResponse.from_domain(value) for value in await service().list()]

    @router.post("", status_code=201)
    async def create_knowledge_base(body: KnowledgeBaseCreateRequest) -> KnowledgeBaseResponse:
        try:
            value = await service().create(body.name, body.description)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return KnowledgeBaseResponse.from_domain(value)

    @router.get("/attachment-types")
    async def attachment_types() -> dict[str, list[str]]:
        return {"extensions": await service().supported_extensions()}

    @router.patch("/{knowledge_base_id}")
    async def update_knowledge_base(
        knowledge_base_id: str, body: KnowledgeBaseUpdateRequest
    ) -> KnowledgeBaseResponse:
        try:
            value = await service().update(knowledge_base_id, body.name, body.description)
        except KnowledgeBaseNotFoundError as exc:
            raise HTTPException(status_code=404, detail="knowledge_base_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return KnowledgeBaseResponse.from_domain(value)

    @router.delete("/{knowledge_base_id}")
    async def delete_knowledge_base(knowledge_base_id: str) -> dict[str, str]:
        try:
            await service().delete(knowledge_base_id)
        except KnowledgeBaseNotFoundError as exc:
            raise HTTPException(status_code=404, detail="knowledge_base_not_found") from exc
        return {"status": "deleted", "knowledge_base_id": knowledge_base_id}

    @router.get("/{knowledge_base_id}/documents")
    async def list_documents(knowledge_base_id: str) -> list[AttachmentResponse]:
        try:
            values = await service().list_documents(knowledge_base_id)
        except KnowledgeBaseNotFoundError as exc:
            raise HTTPException(status_code=404, detail="knowledge_base_not_found") from exc
        return [AttachmentResponse.from_domain(value) for value in values]

    @router.post("/{knowledge_base_id}/documents", status_code=202)
    async def upload_documents(
        knowledge_base_id: str,
        files: list[UploadFile] = File(...),
    ) -> list[AttachmentResponse]:
        if not files:
            raise HTTPException(status_code=400, detail="no_files")
        created = []
        try:
            for upload in files:
                async def chunks():
                    while True:
                        chunk = await upload.read(1024 * 1024)
                        if not chunk:
                            break
                        yield chunk

                created.append(
                    await service().upload_document(
                        knowledge_base_id,
                        filename=upload.filename or "upload.bin",
                        mime_type=upload.content_type or "application/octet-stream",
                        chunks=chunks(),
                    )
                )
        except KnowledgeBaseNotFoundError as exc:
            raise HTTPException(status_code=404, detail="knowledge_base_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            for upload in files:
                await upload.close()
        return [AttachmentResponse.from_domain(value) for value in created]

    @router.get("/{knowledge_base_id}/documents/{attachment_id}/versions")
    async def list_versions(knowledge_base_id: str, attachment_id: str) -> list[AttachmentResponse]:
        try:
            values = await service().list_versions(knowledge_base_id, attachment_id)
        except (KnowledgeBaseNotFoundError, KnowledgeDocumentNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="knowledge_document_not_found") from exc
        return [AttachmentResponse.from_domain(value) for value in values]

    @router.delete("/{knowledge_base_id}/documents/{attachment_id}")
    async def delete_document(knowledge_base_id: str, attachment_id: str) -> dict[str, str]:
        try:
            await service().delete_document(knowledge_base_id, attachment_id)
        except (KnowledgeBaseNotFoundError, KnowledgeDocumentNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="knowledge_document_not_found") from exc
        return {"status": "deleted", "attachment_id": attachment_id}

    return router
