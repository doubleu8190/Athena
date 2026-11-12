"""会话文件 API 的对象存储故障与下载预读边界测试。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast

from auth_support import AuthTestClient, make_auth_service
from fastapi.testclient import TestClient

from adapter.file.storage import FileStorageError
from app.file import SessionFileService
from main import create_app


class _FileService:
    """只实现下载路由所需方法的文件服务替身。"""

    def __init__(
        self,
        *,
        prefetch_error: FileStorageError | None = None,
    ) -> None:
        self.prefetch_error = prefetch_error
        self.iterations = 0
        self.closed = False

    async def get(self, *_: object) -> object:
        return SimpleNamespace(
            filename="report.txt",
            mime_type="text/plain",
            object_key="session-files/aa/file_one",
            size_bytes=11,
        )

    async def upload(self, *_: object, **__: object) -> object:
        raise FileStorageError("file_storage_unavailable")

    async def iter_download(self, _: object) -> AsyncGenerator[bytes, None]:
        self.iterations += 1
        try:
            if self.prefetch_error is not None:
                raise self.prefetch_error
            yield b"first"
            yield b"second"
        finally:
            self.closed = True


def _app(service: _FileService) -> Any:
    return create_app(
        auth_service=make_auth_service(),
        session_file_service=cast(SessionFileService, service),
    )


def test_download_prefetch_maps_missing_object_to_503_before_success() -> None:
    service = _FileService(
        prefetch_error=FileStorageError("file_storage_not_found")
    )

    with AuthTestClient(_app(service)) as client:
        response = client.get(
            "/api/v1/sessions/session_one/files/file_one/download"
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "file_storage_unavailable"}
    assert service.iterations == 1
    assert service.closed is True


def test_download_prefetch_keeps_streaming_response_and_closes_iterator() -> None:
    service = _FileService()

    with AuthTestClient(_app(service)) as client:
        response = client.get(
            "/api/v1/sessions/session_one/files/file_one/download"
        )

    assert response.status_code == 200
    assert response.content == b"firstsecond"
    assert service.closed is True


def test_upload_storage_runtime_error_maps_to_503_without_masking_other_errors() -> None:
    service = _FileService()

    with AuthTestClient(_app(service)) as client:
        response = client.post(
            "/api/v1/sessions/session_one/files",
            headers={"Idempotency-Key": "upload-one"},
            files={"file": ("report.txt", b"hello", "text/plain")},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "file_storage_unavailable"}


def test_file_storage_handler_does_not_mask_unrelated_runtime_error() -> None:
    """全局处理器只匹配 FileStorageError，其他 RuntimeError 仍返回 500。"""
    app = _app(_FileService())

    async def fail() -> None:
        raise RuntimeError("unrelated")

    app.add_api_route("/test/unrelated-runtime", fail, methods=["GET"])
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test/unrelated-runtime")

    assert response.status_code == 500
