"""知识库目录、文本导入和搜索 HTTP 接口。"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)

from api.converter.knowledge import (
    knowledge_base_to_response,
    knowledge_document_to_response,
    knowledge_search_hit_to_response,
    knowledge_version_to_response,
)
from api.dto.knowledge import (
    KnowledgeBaseListResponse,
    KnowledgeBasePatchRequest,
    KnowledgeBaseRequest,
    KnowledgeBaseResponse,
    KnowledgeDocumentListResponse,
    KnowledgeDocumentPatchRequest,
    KnowledgeDocumentResponse,
    KnowledgeImportResponse,
    KnowledgeSearchHit,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeTextRequest,
    KnowledgeVersionResponse,
)
from api.security import CurrentAuth
from app.knowledge.service import (
    KnowledgeInputError,
    KnowledgeNotFoundError,
    KnowledgePermissionDeniedError,
    KnowledgeSearchUnavailableError,
    KnowledgeService,
)
from common.unset import UNSET, UnsetType
from domain.access import VisibilityGrantInput, VisibilityScope
from domain.knowledge.model import (
    OriginalStorageError,
)
from domain.knowledge.ports import DocumentFileValidator


def build_knowledge_router(
    service: KnowledgeService, validator: DocumentFileValidator
) -> APIRouter:
    """将知识库用例绑定到版本化 API。"""
    router = APIRouter(
        prefix="/api/v1/knowledge-bases",
        tags=["knowledge"],
    )

    @router.post("", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
    async def create_base(
        request: KnowledgeBaseRequest, auth: CurrentAuth
    ) -> KnowledgeBaseResponse:
        try:
            created = await service.create_base(
                auth.user.id,
                request.name,
                request.description,
                visibility_scope=VisibilityScope(request.visibility_scope),
                owner_department_id=request.owner_department_id,
                visibility_grants=(
                    None
                    if request.visibility_grants is None
                    else tuple(item.to_domain() for item in request.visibility_grants)
                ),
            )
            return _base_response(created)
        except KnowledgePermissionDeniedError as error:
            raise _forbidden(error) from error
        except KnowledgeInputError as error:
            raise _bad_request(error) from error

    @router.get("", response_model=KnowledgeBaseListResponse)
    async def list_bases(
        auth: CurrentAuth,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        visibility_scope: Annotated[
            Literal["company", "department", "personal"] | None, Query()
        ] = None,
        owner_department_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    ) -> KnowledgeBaseListResponse:
        try:
            items = await service.list_bases(
                auth.user.id,
                page=page,
                page_size=page_size,
                visibility_scope=(
                    None if visibility_scope is None else VisibilityScope(visibility_scope)
                ),
                owner_department_id=owner_department_id,
            )
        except KnowledgeInputError as error:
            raise _bad_request(error) from error
        return KnowledgeBaseListResponse(
            items=[_base_response(item) for item in items], page=page, page_size=page_size
        )

    @router.get("/{base_id}", response_model=KnowledgeBaseResponse)
    async def get_base(base_id: str, auth: CurrentAuth) -> KnowledgeBaseResponse:
        try:
            return _base_response(await service.get_base(auth.user.id, base_id))
        except KnowledgeNotFoundError as error:
            raise _not_found(error) from error

    @router.patch("/{base_id}", response_model=KnowledgeBaseResponse)
    async def patch_base(
        base_id: str, request: KnowledgeBasePatchRequest, auth: CurrentAuth
    ) -> KnowledgeBaseResponse:
        fields = request.model_fields_set
        try:
            if "visibility_scope" in fields and request.visibility_scope is None:
                raise KnowledgeInputError("visibility_scope_invalid")
            visibility_scope_arg: VisibilityScope | UnsetType = (
                VisibilityScope(request.visibility_scope)
                if "visibility_scope" in fields and request.visibility_scope is not None
                else UNSET
            )
            owner_department_arg: str | None | UnsetType = (
                request.owner_department_id if "owner_department_id" in fields else UNSET
            )
            visibility_grants_arg: tuple[VisibilityGrantInput, ...] | None | UnsetType = (
                (
                    None
                    if request.visibility_grants is None
                    else tuple(item.to_domain() for item in request.visibility_grants)
                )
                if "visibility_grants" in fields
                else UNSET
            )
            updated = await service.rename_base(
                auth.user.id,
                base_id,
                request.name,
                request.description,
                visibility_scope=visibility_scope_arg,
                owner_department_id=owner_department_arg,
                visibility_grants=visibility_grants_arg,
            )
            return _base_response(updated)
        except KnowledgeNotFoundError as error:
            raise _not_found(error) from error
        except KnowledgePermissionDeniedError as error:
            raise _forbidden(error) from error
        except KnowledgeInputError as error:
            raise _bad_request(error) from error

    @router.delete(
        "/{base_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_base(base_id: str, auth: CurrentAuth) -> Response:
        try:
            await service.delete_base(auth.user.id, base_id)
        except KnowledgeNotFoundError as error:
            raise _not_found(error) from error
        except KnowledgePermissionDeniedError as error:
            raise _forbidden(error) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/{base_id}/documents/text",
        response_model=KnowledgeImportResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_text(
        base_id: str,
        request: KnowledgeTextRequest,
        auth: CurrentAuth,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> KnowledgeImportResponse:
        try:
            document, version_id = await service.create_text_document(
                auth.user.id,
                base_id,
                request.title,
                request.text,
                idempotency_key,
            )
        except KnowledgeNotFoundError as error:
            raise _not_found(error) from error
        except KnowledgePermissionDeniedError as error:
            raise _forbidden(error) from error
        except LookupError as error:
            raise _not_found(error) from error
        except ValueError as error:
            if str(error) == "idempotency_conflict":
                raise HTTPException(status_code=409, detail="idempotency_conflict") from error
            raise _bad_request(error) from error
        return KnowledgeImportResponse(
            document_id=document.id,
            version_id=version_id,
            state="queued",
            status_url=f"/api/v1/knowledge-bases/{base_id}/documents/{document.id}",
        )

    @router.post(
        "/{base_id}/documents/upload",
        response_model=KnowledgeImportResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def upload_document(
        base_id: str,
        auth: CurrentAuth,
        title: Annotated[str, Form(min_length=1, max_length=255)],
        file: Annotated[UploadFile, File()],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> KnowledgeImportResponse:
        from app.knowledge.service import MAX_FILE_BYTES

        try:
            content = await _stage_upload(file, file.filename or "", MAX_FILE_BYTES, validator)
            document, version_id = await service.create_file_document(
                auth.user.id,
                base_id,
                title,
                file.filename or "",
                bytes(content),
                idempotency_key,
            )
        except KnowledgeNotFoundError as error:
            raise _not_found(error) from error
        except KnowledgePermissionDeniedError as error:
            raise _forbidden(error) from error
        except LookupError as error:
            raise _not_found(error) from error
        except OriginalStorageError as error:
            raise _storage_unavailable() from error
        except ValueError as error:
            if str(error) == "idempotency_conflict":
                raise HTTPException(status_code=409, detail="idempotency_conflict") from error
            raise _upload_error(error) from error
        return KnowledgeImportResponse(
            document_id=document.id,
            version_id=version_id,
            state="queued",
            status_url=f"/api/v1/knowledge-bases/{base_id}/documents/{document.id}",
        )

    @router.post(
        "/{base_id}/documents/{document_id}/versions/upload",
        response_model=KnowledgeImportResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def replace_upload(
        base_id: str,
        document_id: str,
        auth: CurrentAuth,
        file: Annotated[UploadFile, File()],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> KnowledgeImportResponse:
        from app.knowledge.service import MAX_FILE_BYTES

        try:
            content = await _stage_upload(file, file.filename or "", MAX_FILE_BYTES, validator)
            document, version_id = await service.replace_file_document(
                auth.user.id,
                base_id,
                document_id,
                file.filename or "",
                bytes(content),
                idempotency_key,
            )
        except KnowledgeNotFoundError as error:
            raise _not_found(error) from error
        except KnowledgePermissionDeniedError as error:
            raise _forbidden(error) from error
        except LookupError as error:
            raise _not_found(error) from error
        except OriginalStorageError as error:
            raise _storage_unavailable() from error
        except KnowledgeInputError as error:
            if str(error) in {"document_update_in_progress", "idempotency_conflict"}:
                raise HTTPException(status_code=409, detail=str(error)) from error
            raise _upload_error(error) from error
        except ValueError as error:
            raise _upload_error(error) from error
        return KnowledgeImportResponse(
            document_id=document.id,
            version_id=version_id,
            state="queued",
            status_url=f"/api/v1/knowledge-bases/{base_id}/documents/{document.id}",
        )

    @router.post(
        "/{base_id}/documents/{document_id}/versions/text",
        response_model=KnowledgeImportResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def replace_text(
        base_id: str,
        document_id: str,
        request: KnowledgeTextRequest,
        auth: CurrentAuth,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> KnowledgeImportResponse:
        try:
            document, version_id = await service.replace_text_document(
                auth.user.id,
                base_id,
                document_id,
                request.text,
                idempotency_key,
            )
        except KnowledgeNotFoundError as error:
            raise _not_found(error) from error
        except KnowledgePermissionDeniedError as error:
            raise _forbidden(error) from error
        except LookupError as error:
            raise _not_found(error) from error
        except KnowledgeInputError as error:
            if str(error) == "document_update_in_progress":
                raise HTTPException(status_code=409, detail=str(error)) from error
            if str(error) == "idempotency_conflict":
                raise HTTPException(status_code=409, detail=str(error)) from error
            raise _bad_request(error) from error
        return KnowledgeImportResponse(
            document_id=document.id,
            version_id=version_id,
            state="queued",
            status_url=f"/api/v1/knowledge-bases/{base_id}/documents/{document.id}",
        )

    @router.get("/{base_id}/documents", response_model=KnowledgeDocumentListResponse)
    async def list_documents(
        base_id: str,
        auth: CurrentAuth,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> KnowledgeDocumentListResponse:
        try:
            items = await service.list_documents(
                auth.user.id, base_id, page=page, page_size=page_size
            )
        except KnowledgeNotFoundError as error:
            raise _not_found(error) from error
        except KnowledgeInputError as error:
            raise _bad_request(error) from error
        return KnowledgeDocumentListResponse(
            items=[_document_response(item) for item in items], page=page, page_size=page_size
        )

    @router.get("/{base_id}/documents/{document_id}", response_model=KnowledgeDocumentResponse)
    async def get_document(
        base_id: str, document_id: str, auth: CurrentAuth
    ) -> KnowledgeDocumentResponse:
        try:
            return _document_response(
                await service.get_document(auth.user.id, base_id, document_id)
            )
        except KnowledgeNotFoundError as error:
            raise _not_found(error) from error

    @router.get(
        "/{base_id}/documents/{document_id}/versions",
        response_model=list[KnowledgeVersionResponse],
    )
    async def list_versions(
        base_id: str, document_id: str, auth: CurrentAuth
    ) -> list[KnowledgeVersionResponse]:
        try:
            versions = await service.list_versions(auth.user.id, base_id, document_id)
        except KnowledgeNotFoundError as error:
            raise _not_found(error) from error
        return [knowledge_version_to_response(item) for item in versions]

    @router.post(
        "/{base_id}/documents/{document_id}/versions/{version_id}/retry",
        response_model=KnowledgeVersionResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def retry_version(
        base_id: str, document_id: str, version_id: str, auth: CurrentAuth
    ) -> KnowledgeVersionResponse:
        try:
            item = await service.retry_version(auth.user.id, base_id, document_id, version_id)
        except KnowledgeNotFoundError as error:
            raise _not_found(error) from error
        except KnowledgePermissionDeniedError as error:
            raise _forbidden(error) from error
        except KnowledgeInputError as error:
            if str(error) == "document_update_in_progress":
                raise HTTPException(status_code=409, detail=str(error)) from error
            raise _bad_request(error) from error
        return knowledge_version_to_response(item)

    @router.patch("/{base_id}/documents/{document_id}", response_model=KnowledgeDocumentResponse)
    async def patch_document(
        base_id: str, document_id: str, request: KnowledgeDocumentPatchRequest, auth: CurrentAuth
    ) -> KnowledgeDocumentResponse:
        try:
            document = await service.rename_document(
                auth.user.id,
                base_id,
                document_id,
                request.title,
            )
        except KnowledgeNotFoundError as error:
            raise _not_found(error) from error
        except KnowledgePermissionDeniedError as error:
            raise _forbidden(error) from error
        except KnowledgeInputError as error:
            raise _bad_request(error) from error
        return _document_response(document)

    @router.delete(
        "/{base_id}/documents/{document_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_document(base_id: str, document_id: str, auth: CurrentAuth) -> Response:
        try:
            await service.delete_document(auth.user.id, base_id, document_id)
        except KnowledgeNotFoundError as error:
            raise _not_found(error) from error
        except KnowledgePermissionDeniedError as error:
            raise _forbidden(error) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/{base_id}/search", response_model=KnowledgeSearchResponse)
    async def search(
        base_id: str, request: KnowledgeSearchRequest, auth: CurrentAuth
    ) -> KnowledgeSearchResponse:
        try:
            result = await service.search(
                auth.user.id, [base_id], request.query, limit=request.limit
            )
        except LookupError as error:
            raise _not_found(error) from error
        except KnowledgeSearchUnavailableError as error:
            raise HTTPException(status_code=503, detail="knowledge_search_unavailable") from error
        except KnowledgeInputError as error:
            raise _bad_request(error) from error
        return KnowledgeSearchResponse(
            hits=[knowledge_search_hit_to_response(hit) for hit in result.hits],
            channels=list(result.channels),
            degraded_reasons=list(result.degraded_reasons),
        )

    @router.get(
        "/{base_id}/documents/{document_id}/versions/{version_id}/source",
        response_class=Response,
        responses={
            200: {
                "description": "Original knowledge document binary content",
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"},
                    }
                },
            }
        },
    )
    async def download_source(
        base_id: str, document_id: str, version_id: str, auth: CurrentAuth
    ) -> Response:
        try:
            filename, media_type, content = await service.source(
                auth.user.id,
                base_id,
                document_id,
                version_id,
            )
        except KnowledgeNotFoundError as error:
            raise _not_found(error) from error
        except OriginalStorageError as error:
            raise _storage_unavailable() from error
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/{base_id}/chunks/{chunk_id}", response_model=KnowledgeSearchHit)
    async def get_chunk(base_id: str, chunk_id: str, auth: CurrentAuth) -> KnowledgeSearchHit:
        try:
            hit = await service.chunk(auth.user.id, base_id, chunk_id)
        except KnowledgeNotFoundError as error:
            raise _not_found(error) from error
        return knowledge_search_hit_to_response(hit)

    return router


_base_response = knowledge_base_to_response
_document_response = knowledge_document_to_response


async def _stage_upload(
    file: UploadFile,
    filename: str,
    maximum: int,
    validator: DocumentFileValidator,
) -> bytes:
    """限制上传并先校验暂存文件，再读取受限大小内容供原件存储。"""
    size = 0
    with tempfile.NamedTemporaryFile(mode="w+b", suffix=".upload", delete=True) as staged:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > maximum:
                raise HTTPException(status_code=413, detail="document_file_too_large")
            await asyncio.to_thread(staged.write, chunk)
        staged.flush()
        # 校验器由组合根注入，API 层不直接依赖 infrastructure 实现。
        await asyncio.to_thread(
            validator.validate_file,
            filename,
            Path(staged.name),
            max_bytes=maximum,
        )
        staged.seek(0)
        return await asyncio.to_thread(staged.read)


def _bad_request(error: Exception) -> HTTPException:
    code = str(error)
    if code in {"knowledge_storage_quota_exceeded", "knowledge_document_quota_exceeded"}:
        return HTTPException(status_code=429, detail=code)
    return HTTPException(status_code=422, detail=str(error))


def _upload_error(error: ValueError) -> HTTPException:
    code = str(error)
    if code in {
        "document_type_unsupported",
        "knowledge_file_type_unsupported",
        "knowledge_legacy_office_unsupported",
    }:
        return HTTPException(status_code=415, detail=code)
    if code in {
        "knowledge_file_size_invalid",
        "knowledge_archive_expanded_size_limit",
        "knowledge_extracted_text_limit",
    }:
        return HTTPException(status_code=413, detail=code)
    return _bad_request(error)


def _not_found(error: Exception) -> HTTPException:
    return HTTPException(status_code=404, detail=str(error))


def _forbidden(error: Exception) -> HTTPException:
    return HTTPException(status_code=403, detail=str(error) or "knowledge_permission_denied")


def _storage_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="knowledge_storage_unavailable")
