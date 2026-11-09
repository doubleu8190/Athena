"""Real MinIO smoke tests for the original-store adapter."""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime
from secrets import token_hex

import pytest

from adapter.knowledge.storage import MinioOriginalStore, MinioStorageConfig
from domain.knowledge.model import OriginalStorageNotFoundError

_MINIO_ENV = (
    "ATHENA_TEST_MINIO_ENDPOINT",
    "ATHENA_TEST_MINIO_ACCESS_KEY",
    "ATHENA_TEST_MINIO_SECRET_KEY",
    "ATHENA_TEST_MINIO_BUCKET",
)
_CONFIGURED = all(os.getenv(name) for name in _MINIO_ENV)
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _CONFIGURED,
        reason="isolated test MinIO endpoint, credentials, and bucket are not configured",
    ),
]


def _config() -> MinioStorageConfig:
    bucket = os.environ["ATHENA_TEST_MINIO_BUCKET"]
    production_bucket = os.getenv("ATHENA_KNOWLEDGE_MINIO_BUCKET")
    if production_bucket and production_bucket == bucket:
        raise AssertionError("test MinIO bucket must differ from the production bucket")
    secure = os.getenv("ATHENA_TEST_MINIO_SECURE", "true").strip().lower()
    if secure not in {"true", "1", "yes", "on", "false", "0", "no", "off"}:
        raise AssertionError("ATHENA_TEST_MINIO_SECURE_invalid")
    return MinioStorageConfig(
        endpoint=os.environ["ATHENA_TEST_MINIO_ENDPOINT"],
        access_key=os.environ["ATHENA_TEST_MINIO_ACCESS_KEY"],
        secret_key=os.environ["ATHENA_TEST_MINIO_SECRET_KEY"],
        bucket=bucket,
        secure=secure in {"true", "1", "yes", "on"},
    )


def _key(content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    source_id = f"ksrc_test_{token_hex(8)}"
    return f"{hashlib.sha256(source_id.encode()).hexdigest()}/{source_id}/{digest[:2]}/{digest}"


def test_real_minio_put_get_delete_and_idempotent_concurrency() -> None:
    async def scenario() -> None:
        store = MinioOriginalStore(_config())
        await store.start()
        created: list[str] = []
        try:
            content = f"real MinIO content {token_hex(8)}".encode()
            key = _key(content)
            created.append(key)
            await asyncio.gather(*(store.put(key, content) for _ in range(4)))
            assert await store.get(key) == content
            assert [
                candidate
                async for candidate in store.iter_orphan_candidates(older_than_seconds=86400)
                if candidate == key
            ] == []

            await store.delete(key)
            with pytest.raises(OriginalStorageNotFoundError):
                await store.get(key)
        finally:
            for key in created:
                try:
                    await store.delete(key)
                except Exception:
                    pass
            await store.aclose()

    asyncio.run(scenario())


def test_real_minio_round_trips_one_object() -> None:
    async def scenario() -> None:
        config = _config()
        store = MinioOriginalStore(config)
        await store.start()
        content = f"expected {token_hex(8)}".encode()
        key = _key(content)
        try:
            await store.put(key, content)
            assert await store.get(key) == content
        finally:
            await store.delete(key)
            await store.aclose()

    asyncio.run(scenario())


def test_real_minio_timestamp_fields_are_available_for_orphan_filtering() -> None:
    async def scenario() -> None:
        store = MinioOriginalStore(
            _config(),
            clock=lambda: datetime(2099, 1, 1, tzinfo=UTC),
        )
        await store.start()
        content = f"old cutover content {token_hex(8)}".encode()
        key = _key(content)
        try:
            await store.put(key, content)
            candidates = [
                candidate
                async for candidate in store.iter_orphan_candidates(older_than_seconds=86400)
                if candidate == key
            ]
            assert candidates == [key]
        finally:
            await store.delete(key)
            await store.aclose()

    asyncio.run(scenario())
