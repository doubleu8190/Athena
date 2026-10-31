"""Worker 资源协议的 HTTPS PUT/GET 路由。"""

from __future__ import annotations

import re
from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from application.orchestration.worker_resources import WorkerResourceService
from application.orchestration.worker_resource_models import (
    WorkerResourceReferenceModel,
)
from interfaces.http.worker_protocol import (
    WorkerProtocolError,
    WorkerProtocolHttp,
)


def build_worker_resources_router(
    service_factory: Callable[[], WorkerResourceService],
    protocol: WorkerProtocolHttp,
) -> APIRouter:
    """建立經服務身份與 Execution Grant 保護的資源路由。

    参数：
    - `service_factory`（Callable[[], WorkerResourceService]）：提供授權和資源應用用例。
    - `protocol`（WorkerProtocolHttp）：驗證服務 Bearer、版本和幂等 Header 的共用協議端口。

    返回：只包含 S1 Worker 资源 PUT/GET 的 APIRouter。

    异常：此建構函数不会主动抛出业务异常。
    """
    router = APIRouter()

    @router.put("/worker-resources/{resource_id}")
    async def upload_worker_resource(resource_id: str, request: Request):
        """校驗服務身份、执行授權、大小及實際字節摘要後保存資源。

        参数：
        - `resource_id`（str）：S1 URL 中的資源 UUID 原始文本。
        - `request`（Request）：包含原始字節及協議 Header 的 HTTP 請求。

        返回：首次建立時 HTTP 201，精確内容重放時 HTTP 200 的 ResourceReference。

        异常：WorkerProtocolError 表示身份、Header、授权、大小、摘要或资源ID衝突。
        """
        principal = protocol.authenticate(request)
        normalized_id = _normalize_uuid(resource_id)
        service = service_factory()
        grant_token = _single_header(request, "x-athena-execution-grant")
        content_type = _single_header(request, "content-type")
        media_type = content_type.strip()
        if not media_type or "," in media_type:
            raise WorkerProtocolError(
                "UNSUPPORTED_MEDIA_TYPE",
                415,
                "operator_action",
                "a single resource media type is required",
            )
        digest = _single_header(request, "content-sha256")
        _require_digest(digest)
        _validate_content_length(request, service.max_bytes)
        protocol.require_idempotency(request, resource_id)
        try:
            result = await service.put_resource(
                service_principal=principal,
                grant_token=grant_token,
                resource_id=normalized_id,
                media_type=media_type,
                expected_sha256=digest,
                file_name=None,
                chunks=request.stream(),
            )
        except ValueError as exc:
            raise _resource_error(exc) from exc
        except RuntimeError as exc:
            raise WorkerProtocolError(
                "INTERNAL_ERROR", 500, "retry_same_command", "resource storage is temporarily unavailable"
            ) from exc
        response_model = WorkerResourceReferenceModel.model_validate(
            {
                "resource_id": result.reference.resource_id,
                "media_type": result.reference.media_type,
                "size_bytes": result.reference.size_bytes,
                "sha256": result.reference.sha256,
                "file_name": result.reference.file_name,
            }
        )
        return JSONResponse(
            status_code=201 if result.disposition == "created" else 200,
            content=response_model.model_dump(mode="json", exclude_none=True),
        )

    @router.get("/worker-resources/{resource_id}")
    async def download_worker_resource(resource_id: str, request: Request):
        """驗證 Grant 後以流方式返回授權的原始資源字節。

        参数：
        - `resource_id`（str）：S1 URL 中的資源 UUID 原始文本。
        - `request`（Request）：包含雙重服務身份與執行授权的 HTTP 請求。

        返回：含原媒體類型、長度和 SHA-256 Header 的原始資源回應。

        异常：WorkerProtocolError 表示身份、簽名、執行範圍或資源授權失敗。
        """
        principal = protocol.authenticate(request)
        normalized_id = _normalize_uuid(resource_id)
        service = service_factory()
        grant_token = _single_header(request, "x-athena-execution-grant")
        try:
            download = await service.get_resource(
                service_principal=principal,
                grant_token=grant_token,
                resource_id=normalized_id,
            )
        except ValueError as exc:
            raise _resource_error(exc) from exc
        except RuntimeError as exc:
            raise WorkerProtocolError(
                "INTERNAL_ERROR", 500, "retry_same_command", "resource storage is temporarily unavailable"
            ) from exc
        return FileResponse(
            download.path,
            media_type=download.reference.media_type,
            headers={
                "Content-SHA256": download.reference.sha256,
                "Content-Length": str(download.reference.size_bytes),
            },
        )

    return router


def _single_header(request: Request, name: str) -> str:
    """读取且只接受唯一、非空的协议 Header。"""
    values = request.headers.getlist(name)
    if len(values) != 1 or not values[0]:
        raise WorkerProtocolError(
            "MALFORMED_REQUEST", 400, "operator_action", f"{name} must appear once"
        )
    return values[0]


_UUID_FORMAT = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _normalize_uuid(value: str) -> str:
    """验证 S1 UUID 连字符格式，再生成数据库使用的小写规范形式。"""
    if not _UUID_FORMAT.fullmatch(value):
        raise WorkerProtocolError(
            "MALFORMED_REQUEST", 400, "operator_action", "resource ID must be an S1 UUID"
        )
    return str(UUID(value))


def _require_digest(value: str) -> None:
    """确认资源摘要 Header 是 64 位小写十六进制。"""
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise WorkerProtocolError(
            "VALIDATION_ERROR", 422, "operator_action", "Content-SHA256 is invalid"
        )


def _validate_content_length(request: Request, max_bytes: int) -> None:
    """在开始读流之前拒绝重复、非法或超过资源上限的长度头。"""
    values = request.headers.getlist("content-length")
    if not values:
        return
    if len(values) != 1:
        raise WorkerProtocolError(
            "MALFORMED_REQUEST", 400, "operator_action", "Content-Length must appear once"
        )
    try:
        size_bytes = int(values[0])
    except ValueError as exc:
        raise WorkerProtocolError(
            "MALFORMED_REQUEST", 400, "operator_action", "Content-Length is invalid"
        ) from exc
    if size_bytes < 0:
        raise WorkerProtocolError(
            "MALFORMED_REQUEST", 400, "operator_action", "Content-Length is invalid"
        )
    if size_bytes > max_bytes:
        raise WorkerProtocolError(
            "PAYLOAD_TOO_LARGE", 413, "operator_action", "resource exceeds the configured size limit"
        )


def _resource_error(error: ValueError) -> WorkerProtocolError:
    """将资源应用错误映射为 S1 稳定 HTTP 错误。"""
    code = str(error)
    if code.startswith("worker_resource_id_conflict"):
        return WorkerProtocolError(
            "RESOURCE_ID_CONFLICT", 409, "operator_action", "resource ID already stores other bytes"
        )
    if code.startswith("worker_resource_digest_mismatch"):
        return WorkerProtocolError(
            "RESOURCE_DIGEST_MISMATCH", 400, "operator_action", "resource digest does not match bytes"
        )
    if code.startswith("worker_resource_too_large"):
        return WorkerProtocolError(
            "PAYLOAD_TOO_LARGE", 413, "operator_action", "resource exceeds the configured size limit"
        )
    if (
        "forbidden" in code
        or "principal_mismatch" in code
        or "scope_" in code
        or code.startswith("worker_execution_grant_")
    ):
        return WorkerProtocolError(
            "FORBIDDEN", 403, "operator_action", "execution grant does not authorize this resource"
        )
    if "unknown" in code:
        return WorkerProtocolError(
            "EXECUTION_NOT_FOUND", 404, "operator_action", "resource or execution was not found"
        )
    return WorkerProtocolError(
        "VALIDATION_ERROR", 422, "operator_action", "resource request is invalid"
    )


__all__ = ["build_worker_resources_router"]
