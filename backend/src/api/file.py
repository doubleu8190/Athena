"""当前用户会话文件上传、列表、元数据和流式下载接口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Path,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse

from api.converter.file import session_file_page_to_response, session_file_to_response
from api.dto.file import SessionFileListResponse, SessionFileResponse
from api.security import CurrentAuth, verify_request_origin
from api.upload import UploadTooLargeError, staged_upload
from app.file import FileInputError, SessionFileService
from domain.file import MAX_SESSION_FILE_BYTES
from domain.session import SessionNotFoundError


def build_session_file_router(service: SessionFileService) -> APIRouter:
    """绑定当前登录用户的会话文件接口。"""
    router = APIRouter(
        prefix="/api/v1/sessions/{session_id}/files",
        tags=["session-files"],
        dependencies=[Depends(verify_request_origin)],
    )

    @router.post("", response_model=SessionFileResponse, status_code=status.HTTP_201_CREATED)
    async def upload_file(
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        current_auth: CurrentAuth,
        file: Annotated[UploadFile, File()],
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=128,
            ),
        ],
    ) -> SessionFileResponse:
        """流式接收单个文件，类型校验和对象写入成功后才发布元数据。"""
        try:
            async with staged_upload(
                file,
                maximum_bytes=MAX_SESSION_FILE_BYTES,
            ) as path:
                result = await service.upload(
                    session_id,
                    current_auth.user.id,
                    idempotency_key=idempotency_key,
                    filename=file.filename or "",
                    declared_mime=file.content_type,
                    path=path,
                )
                return session_file_to_response(result)
        except UploadTooLargeError as error:
            raise HTTPException(413, "document_file_too_large") from error
        except SessionNotFoundError as error:
            raise HTTPException(404, "session_not_found") from error
        except FileInputError as error:
            if error.code == "idempotency_conflict":
                raise HTTPException(409, error.code) from error
            raise HTTPException(422, error.code) from error
        except ValueError as error:
            code = str(error)
            if code == "session_file_quota_exceeded":
                raise HTTPException(409, code) from error
            if code == "idempotency_conflict":
                raise HTTPException(409, code) from error
            raise HTTPException(422, "file_upload_invalid") from error
        except OSError as error:
            raise HTTPException(503, "file_storage_unavailable") from error

    @router.get("", response_model=SessionFileListResponse)
    async def list_files(
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        current_auth: CurrentAuth,
        after_file_id: Annotated[str | None, Query(max_length=128)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> SessionFileListResponse:
        """读取当前可见会话内上传与产物文件。"""
        try:
            page = await service.list(
                session_id,
                current_auth.user.id,
                after_file_id=after_file_id,
                limit=limit,
            )
            return session_file_page_to_response(page)
        except SessionNotFoundError as error:
            raise HTTPException(404, "session_not_found") from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.get("/{file_id}", response_model=SessionFileResponse)
    async def get_file(
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        file_id: Annotated[str, Path(min_length=1, max_length=128)],
        current_auth: CurrentAuth,
    ) -> SessionFileResponse:
        """实时校验会话归属后读取安全元数据。"""
        try:
            return session_file_to_response(
                await service.get(session_id, current_auth.user.id, file_id)
            )
        except FileInputError as error:
            raise HTTPException(404, error.code) from error

    @router.get("/{file_id}/download")
    async def download_file(
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        file_id: Annotated[str, Path(min_length=1, max_length=128)],
        current_auth: CurrentAuth,
    ) -> StreamingResponse:
        """实时鉴权后流式下载，强制 attachment 并禁止 MIME 嗅探。"""
        try:
            file = await service.get(session_id, current_auth.user.id, file_id)
        except FileInputError as error:
            raise HTTPException(404, error.code) from error

        stream = service.iter_download(file)
        try:
            # 先实际打开对象并读取首块，避免对象缺失或存储故障时已发送 200。
            first_chunk = await anext(stream)
        except StopAsyncIteration:
            first_chunk = None
        except BaseException:
            try:
                await stream.aclose()
            except Exception:
                pass
            raise

        async def body() -> AsyncIterator[bytes]:
            try:
                if first_chunk is not None:
                    yield first_chunk
                async for chunk in stream:
                    yield chunk
            finally:
                # 客户端提前断开时也必须关闭 MinIO HTTP 响应。
                await stream.aclose()

        fallback = "".join(
            character
            if character.isascii() and (character.isalnum() or character in "._- ")
            else "_"
            for character in file.filename
        ).strip() or "download"
        encoded = quote(file.filename, safe="")
        return StreamingResponse(
            body(),
            media_type=file.mime_type,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'
                ),
                "Content-Length": str(file.size_bytes),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
        )

    return router


__all__ = ["build_session_file_router"]
