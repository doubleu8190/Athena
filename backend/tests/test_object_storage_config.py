"""统一对象存储配置 ATHENA_STORAGE_* 与单桶多业务前缀隔离单元测试。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from typing import Any, BinaryIO, cast

import pytest
from minio.error import S3Error

from adapter.file.storage import MinioFileObjectStore
from adapter.knowledge.storage import KNOWLEDGE_OBJECT_PREFIX, MinioOriginalStore
from infrastructure.storage import (
    ObjectStorageConfig,
    ObjectStorageConfigError,
    has_object_storage_env,
)


@dataclass(slots=True)
class _BucketObject:
    content: bytes
    last_modified: datetime


class _SharedBucketFakeMinio:
    """模拟单桶同时承载 knowledge/ 与文件/技能对象的共享 MinIO 客户端。"""

    def __init__(self, *, now: datetime) -> None:
        self.now = now
        self.objects: dict[str, _BucketObject] = {}
        self.list_prefixes: list[str | None] = []

    def bucket_exists(self, bucket_name: str) -> bool:
        del bucket_name
        return True

    def stat_object(self, bucket_name: str, object_name: str) -> SimpleNamespace:
        del bucket_name
        item = self.objects.get(object_name)
        if item is None:
            raise S3Error(
                response=cast(Any, SimpleNamespace(status=404, data=b"")),
                code="NoSuchKey",
                message="missing",
                resource="/bucket/" + object_name,
                request_id="req",
                host_id="host",
                bucket_name="athena-shared",
                object_name=object_name,
            )
        return SimpleNamespace(size=len(item.content), last_modified=item.last_modified)

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: BinaryIO,
        length: int,
        *,
        content_type: str,
        num_parallel_uploads: int,
    ) -> object:
        del bucket_name, content_type, num_parallel_uploads
        self.objects[object_name] = _BucketObject(data.read(length), self.now)
        return SimpleNamespace(object_name=object_name)

    def get_object(
        self,
        bucket_name: str,
        object_name: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> Any:
        del bucket_name
        item = self.objects.get(object_name)
        if item is None:
            raise S3Error(
                response=cast(Any, SimpleNamespace(status=404, data=b"")),
                code="NoSuchKey",
                message="missing",
                resource="/bucket/" + object_name,
                request_id="req",
                host_id="host",
                bucket_name="athena-shared",
                object_name=object_name,
            )
        sliced = item.content[offset : None if length is None else offset + length]
        stream = BytesIO(sliced)

        class _Resp:
            def read(self, amt: int | None = None) -> bytes:
                return stream.read() if amt is None else stream.read(amt)

            def close(self) -> None:
                stream.close()

            def release_conn(self) -> None:
                return None

        return _Resp()

    def remove_object(self, bucket_name: str, object_name: str) -> object:
        del bucket_name
        self.objects.pop(object_name, None)
        return SimpleNamespace(object_name=object_name)

    def list_objects(
        self,
        bucket_name: str,
        *,
        prefix: str | None = None,
        recursive: bool,
        start_after: str | None = None,
    ) -> Iterator[SimpleNamespace]:
        del bucket_name
        assert recursive is True
        self.list_prefixes.append(prefix)
        for name in sorted(self.objects):
            if prefix is not None and not name.startswith(prefix):
                continue
            if start_after is not None and name <= start_after:
                continue
            yield SimpleNamespace(object_name=name, last_modified=self.objects[name].last_modified)


def test_object_storage_config_parses_athena_storage_env_and_validates() -> None:
    assert has_object_storage_env({}) is False
    assert has_object_storage_env({"ATHENA_STORAGE_BUCKET": "athena-prod"}) is True

    config = ObjectStorageConfig.from_environment(
        {
            "ATHENA_STORAGE_ENDPOINT": "[2001:db8::1]:9000",
            "ATHENA_STORAGE_ACCESS_KEY": "ak-prod",
            "ATHENA_STORAGE_SECRET_KEY": "sk-prod-secret",
            "ATHENA_STORAGE_BUCKET": "athena-prod",
        }
    )
    assert config.endpoint == "[2001:db8::1]:9000"
    assert config.bucket == "athena-prod"
    assert config.secure is True
    assert "sk-prod-secret" not in repr(config)
    assert "ak-prod" not in repr(config)

    with pytest.raises(ObjectStorageConfigError):
        ObjectStorageConfig.from_environment(
            {
                "ATHENA_STORAGE_ENDPOINT": "https://minio.invalid:9000",
                "ATHENA_STORAGE_ACCESS_KEY": "ak",
                "ATHENA_STORAGE_SECRET_KEY": "sk",
                "ATHENA_STORAGE_BUCKET": "athena-prod",
            }
        )
    with pytest.raises(ObjectStorageConfigError):
        ObjectStorageConfig.from_environment(
            {
                "ATHENA_STORAGE_ENDPOINT": "minio.internal:9000",
                "ATHENA_STORAGE_ACCESS_KEY": "ak",
                "ATHENA_STORAGE_SECRET_KEY": "sk",
                "ATHENA_STORAGE_BUCKET": "athena-prod",
                "ATHENA_STORAGE_SECURE": "unknown",
            }
        )


def test_single_bucket_prefix_isolation_between_knowledge_and_files() -> None:
    now = datetime(2026, 11, 20, tzinfo=UTC)
    old_time = now - timedelta(days=3)
    client = _SharedBucketFakeMinio(now=old_time)
    config = ObjectStorageConfig(
        endpoint="minio.internal:9000",
        access_key="ak",
        secret_key="sk",
        bucket="athena-shared",
        secure=True,
    )

    knowledge_content = b"knowledge pdf bytes"
    digest = hashlib.sha256(knowledge_content).hexdigest()
    logical_knowledge_key = f"{'a' * 64}/ksrc_1/{digest[:2]}/{digest}"
    file_key = "session-files/ab/file_001"
    skill_key = f"skills/cd/{'b' * 64}/ver_001.zip"

    async def scenario() -> None:
        knowledge_store = MinioOriginalStore(
            config,
            client=cast(Any, client),
            clock=lambda: now,
        )
        file_store = MinioFileObjectStore(
            config,
            client=cast(Any, client),
            clock=lambda: now,
        )
        await knowledge_store.start()
        await file_store.start()
        try:
            await knowledge_store.put(logical_knowledge_key, knowledge_content)
            await file_store.put_bytes(
                file_key, b"session file bytes", content_type="application/octet-stream"
            )
            await file_store.put_bytes(
                skill_key, b"skill zip bytes", content_type="application/zip"
            )

            # 验证物理键落入各自前缀，且逻辑读取保持洁净。
            assert f"{KNOWLEDGE_OBJECT_PREFIX}{logical_knowledge_key}" in client.objects
            assert logical_knowledge_key not in client.objects
            assert file_key in client.objects
            assert skill_key in client.objects
            assert await knowledge_store.get(logical_knowledge_key) == knowledge_content
            assert await file_store.get_bytes(file_key, maximum_bytes=1024) == b"session file bytes"

            # 知识库孤儿扫描只扫描 prefix="knowledge/"，返回逻辑键，绝不触碰文件/技能对象。
            knowledge_orphans = [
                key
                async for key in knowledge_store.iter_orphan_candidates(older_than_seconds=86400)
            ]
            assert knowledge_orphans == [logical_knowledge_key]
            assert client.list_prefixes[-1] == KNOWLEDGE_OBJECT_PREFIX

            # 文件离线维护扫描绝不将 knowledge/ 对象判为孤儿候选。
            file_entries = [
                entry
                async for entry in file_store.iter_maintenance_candidates(older_than_seconds=86400)
            ]
            candidates = [entry.object_key for entry in file_entries if entry.is_candidate]
            non_candidates = [entry.object_key for entry in file_entries if not entry.is_candidate]
            assert candidates == [file_key, skill_key]
            assert non_candidates == [f"{KNOWLEDGE_OBJECT_PREFIX}{logical_knowledge_key}"]
        finally:
            await knowledge_store.aclose()
            await file_store.aclose()

    asyncio.run(scenario())
