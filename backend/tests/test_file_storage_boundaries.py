"""文件对象存储配置隔离与范围读取边界测试。"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from minio.error import S3Error

from adapter.file.storage import (
    FileStorageError,
    MinioFileObjectStore,
    MinioFileStorageConfig,
)


class _Response:
    """模拟 MinIO 响应并对每次读取计数。"""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.offset = 0
        self.closed = False
        self.released = False

    def read(self, amount: int | None = None) -> bytes:
        if amount is None:
            amount = len(self.content) - self.offset
        chunk = self.content[self.offset : self.offset + amount]
        self.offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class _Client:
    """按对象内容返回响应，或模拟 NoSuchKey。"""

    def __init__(self, content: bytes, *, missing: bool = False) -> None:
        self.content = content
        self.missing = missing
        self.calls: list[tuple[int, int | None]] = []
        self.response: _Response | None = None

    def get_object(
        self,
        bucket_name: str,
        object_name: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> _Response:
        del bucket_name, object_name
        self.calls.append((offset, length))
        if self.missing:
            raise S3Error(
                response=cast(Any, object()),
                code="NoSuchKey",
                message="missing",
                resource="/missing",
                request_id="request",
                host_id="host",
            )
        self.response = _Response(self.content[offset : offset + (length or len(self.content))])
        return self.response


def _store(client: _Client) -> MinioFileObjectStore:
    return MinioFileObjectStore(
        MinioFileStorageConfig(
            endpoint="minio.example.test:443",
            access_key="test-access",
            secret_key="test-secret",
            bucket="athena-files-test",
            secure=True,
        ),
        client=client,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("offset", [0, 7])
def test_get_range_missing_object_is_not_mistaken_for_eof(offset: int) -> None:
    client = _Client(b"", missing=True)

    with pytest.raises(FileStorageError) as error:
        asyncio.run(
            _store(client).get_range(
                "session-files/aa/missing",
                offset=offset,
                length=4,
            )
        )

    assert error.value.code == "file_storage_not_found"


def test_get_range_returns_empty_only_for_true_eof_and_closes_response() -> None:
    client = _Client(b"abc")

    content = asyncio.run(
        _store(client).get_range(
            "session-files/aa/short",
            offset=10,
            length=4,
        )
    )

    assert content == b""
    assert client.response is not None
    assert client.response.closed is True
    assert client.response.released is True


def test_file_storage_config_ignores_legacy_minio_env_vars() -> None:
    legacy_only = {
        "ATHENA_KNOWLEDGE_MINIO_ENDPOINT": "knowledge.example.test:9000",
        "ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY": "knowledge-access",
        "ATHENA_KNOWLEDGE_MINIO_SECRET_KEY": "knowledge-secret",
        "ATHENA_KNOWLEDGE_MINIO_BUCKET": "knowledge-bucket",
        "ATHENA_FILE_MINIO_ENDPOINT": "files.example.test:9000",
        "ATHENA_FILE_MINIO_ACCESS_KEY": "files-access",
        "ATHENA_FILE_MINIO_SECRET_KEY": "files-secret",
        "ATHENA_FILE_MINIO_BUCKET": "files-bucket",
    }

    with pytest.raises(FileStorageError, match="file_storage_configuration_invalid"):
        MinioFileStorageConfig.from_environment(legacy_only)


def test_file_storage_config_reads_unified_storage_variables() -> None:
    config = MinioFileStorageConfig.from_environment(
        {
            "ATHENA_STORAGE_ENDPOINT": "storage.example.test:9000",
            "ATHENA_STORAGE_ACCESS_KEY": "storage-access",
            "ATHENA_STORAGE_SECRET_KEY": "storage-secret",
            "ATHENA_STORAGE_BUCKET": "athena-shared-bucket",
            "ATHENA_STORAGE_SECURE": "false",
            "ATHENA_KNOWLEDGE_MINIO_ENDPOINT": "legacy.example.test:9000",
            "ATHENA_KNOWLEDGE_MINIO_BUCKET": "legacy-bucket",
        }
    )

    assert config.endpoint == "storage.example.test:9000"
    assert config.access_key == "storage-access"
    assert config.secret_key == "storage-secret"
    assert config.bucket == "athena-shared-bucket"
    assert config.secure is False
    assert "storage-secret" not in repr(config)


def test_file_storage_rejects_knowledge_prefix_keys() -> None:
    client = _Client(b"secret")
    store = _store(client)

    with pytest.raises(ValueError, match="file_storage_key_invalid"):
        asyncio.run(store.get_bytes("knowledge/aa/bb", maximum_bytes=16))
    with pytest.raises(ValueError, match="file_storage_key_invalid"):
        asyncio.run(store.put_bytes("knowledge/aa/bb", b"x", content_type="text/plain"))
    with pytest.raises(ValueError, match="file_storage_key_invalid"):
        asyncio.run(store.delete("knowledge/aa/bb"))
