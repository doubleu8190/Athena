"""会话附件 Controller。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from application.files import (
    AttachmentNotFoundError,
    AttachmentService,
    SessionNotFoundError,
)
from domain.files import Attachment


class AttachmentResponse(BaseModel):
    id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    storage_key: str
    status: str
    session_id: str | None
    knowledge_base_id: str | None
    message_id: str | None
    adapter_name: str | None
    adapter_version: str | None
    capabilities: list[str]
    metadata: dict[str, Any]
    error_message: str | None
    created_at: Any
    updated_at: Any

    @classmethod
    def from_domain(cls, value: Attachment) -> "AttachmentResponse":
        return cls(
            id=value.id,
            filename=value.filename,
            mime_type=value.mime_type,
            size_bytes=value.size_bytes,
            sha256=value.sha256,
            storage_key=value.storage_key,
            status=value.status.value,
            session_id=value.session_id,
            knowledge_base_id=value.knowledge_base_id,
            message_id=value.message_id,
            adapter_name=value.adapter_name,
            adapter_version=value.adapter_version,
            capabilities=list(value.capabilities),
            metadata=value.metadata.values,
            error_message=value.error_message,
            created_at=value.created_at,
            updated_at=value.updated_at,
        )


def build_files_router(
    service_factory: Callable[[], AttachmentService],
) -> APIRouter:
    router = APIRouter(prefix="/sessions/{session_id}", tags=["files"])

    def service() -> AttachmentService:
        return service_factory()

    @router.get("/attachments", response_model=list[AttachmentResponse])
    async def list_attachments(session_id: str) -> list[AttachmentResponse]:
        try:
            values = await service().list_for_session(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        return [AttachmentResponse.from_domain(value) for value in values]

    @router.post("/attachments", status_code=202, response_model=list[AttachmentResponse])
    async def upload_attachments(
        session_id: str,
        files: list[UploadFile] = File(...),
    ) -> list[AttachmentResponse]:
        if not files:
            raise HTTPException(status_code=400, detail="no_files")
        created: list[Attachment] = []
        try:
            for upload in files:
                filename = upload.filename or "upload.bin"

                async def chunks():
                    while True:
                        chunk = await upload.read(1024 * 1024)
                        if not chunk:
                            break
                        yield chunk

                created.append(
                    await service().upload(
                        session_id,
                        filename=filename,
                        mime_type=upload.content_type or "application/octet-stream",
                        chunks=chunks(),
                    )
                )
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            for upload in files:
                await upload.close()
        return [AttachmentResponse.from_domain(value) for value in created]

    @router.get("/attachment-types")
    async def supported_attachment_types(session_id: str) -> dict[str, list[str]]:
        try:
            values = await service().supported_extensions(session_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        return {"extensions": values}

    @router.get("/attachments/{attachment_id}", response_model=AttachmentResponse)
    @router.get("/attachments/{file_id}", response_model=AttachmentResponse, include_in_schema=False)
    async def get_attachment(session_id: str, attachment_id: str | None = None, file_id: str | None = None) -> AttachmentResponse:
        attachment_id = attachment_id or file_id
        assert attachment_id is not None
        try:
            value = await service().get(session_id, attachment_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        except AttachmentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="attachment_not_found") from exc
        return AttachmentResponse.from_domain(value)

    @router.delete("/attachments/{attachment_id}")
    @router.delete("/attachments/{file_id}", include_in_schema=False)
    async def delete_attachment(session_id: str, attachment_id: str | None = None, file_id: str | None = None) -> dict[str, str]:
        attachment_id = attachment_id or file_id
        assert attachment_id is not None
        try:
            await service().delete(session_id, attachment_id)
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="session_not_found") from exc
        except AttachmentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="attachment_not_found") from exc
        return {"status": "deleted", "file_id": attachment_id}

    return router
