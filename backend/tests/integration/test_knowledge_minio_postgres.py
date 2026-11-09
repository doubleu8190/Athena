"""Joint real-PostgreSQL and isolated-MinIO knowledge consistency tests."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex
from typing import Any, cast

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from adapter.auth.model import UserModel
from adapter.knowledge.model import KnowledgeJobModel, KnowledgeSourceModel
from adapter.knowledge.repository import KnowledgeRepository
from adapter.knowledge.storage import MinioOriginalStore, MinioStorageConfig
from app.knowledge.service import KnowledgeService
from app.knowledge.worker import KnowledgeWorker
from domain.knowledge.model import (
    Document,
    KnowledgeBase,
    OriginalStorageError,
    OriginalStorageNotFoundError,
    OriginalStorageUnavailableError,
)
from domain.knowledge.ports import (
    ChineseTokenizer,
    DocumentParser,
    EmbeddingPort,
    KnowledgeRepositoryPort,
    ParsedDocument,
    TokenCounterPort,
)

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
_MINIO_ENV = (
    "ATHENA_TEST_MINIO_ENDPOINT",
    "ATHENA_TEST_MINIO_ACCESS_KEY",
    "ATHENA_TEST_MINIO_SECRET_KEY",
    "ATHENA_TEST_MINIO_BUCKET",
)
_MINIO_CONFIGURED = all(os.getenv(name) for name in _MINIO_ENV)
BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
DB_CONNECT_ARGS = {"timeout": 8, "command_timeout": 20}
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DATABASE_URL or not _MINIO_CONFIGURED,
        reason=(
            "joint test requires ATHENA_TEST_DATABASE_URL and all "
            "ATHENA_TEST_MINIO_* endpoint, credentials, and bucket variables"
        ),
    ),
]


class _WhitespaceTokenizer:
    def tokenize(self, value: str) -> tuple[str, ...]:
        return tuple(value.split())


class _WhitespaceCounter:
    def count(self, value: str) -> int:
        return len(value.split())


class _UnusedParser:
    async def parse(self, filename: str, content: bytes) -> ParsedDocument:
        del filename, content
        raise AssertionError("cleanup and orphan tests must not parse documents")


class _UnusedEmbedding:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        del texts
        raise AssertionError("cleanup and orphan tests must not embed documents")


class _RecordingStore:
    """Delegate to real MinIO while recording only the keys created by one test."""

    def __init__(self, store: MinioOriginalStore) -> None:
        self._store = store
        self.put_keys: list[str] = []
        self.delete_keys: list[str] = []

    async def put(self, key: str, content: bytes) -> None:
        self.put_keys.append(key)
        await self._store.put(key, content)

    async def get(self, key: str) -> bytes:
        return await self._store.get(key)

    async def delete(self, key: str) -> None:
        self.delete_keys.append(key)
        await self._store.delete(key)

    async def iter_orphan_candidates(
        self, *, older_than_seconds: int = 86400
    ) -> AsyncIterator[str]:
        async for key in self._store.iter_orphan_candidates(older_than_seconds=older_than_seconds):
            yield key


class _FailOnceDeleteStore(_RecordingStore):
    def __init__(self, store: MinioOriginalStore) -> None:
        super().__init__(store)
        self.fail_next_delete = True

    async def delete(self, key: str) -> None:
        if self.fail_next_delete:
            self.fail_next_delete = False
            raise OriginalStorageUnavailableError()
        await super().delete(key)


class _InjectedCandidateStore(_RecordingStore):
    def __init__(self, store: MinioOriginalStore, keys: tuple[str, ...]) -> None:
        super().__init__(store)
        self.injected_keys = keys

    async def iter_orphan_candidates(
        self, *, older_than_seconds: int = 86400
    ) -> AsyncIterator[str]:
        if older_than_seconds < 86400:
            raise ValueError("knowledge_orphan_grace_period_invalid")
        for key in self.injected_keys:
            yield key


class _CommitThenRaiseRepository:
    def __init__(self, repository: KnowledgeRepository) -> None:
        self._repository = repository
        self.committed_result: tuple[Document, str] | None = None
        self.storage_key: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repository, name)

    async def create_file_document(self, **kwargs: Any) -> tuple[Document, str]:
        result = await self._repository.create_file_document(**kwargs)
        self.committed_result = result
        self.storage_key = str(kwargs["storage_key"])
        raise ConnectionError("simulated lost response after PostgreSQL commit")


def _config() -> MinioStorageConfig:
    bucket = os.environ["ATHENA_TEST_MINIO_BUCKET"]
    production_bucket = os.getenv("ATHENA_KNOWLEDGE_MINIO_BUCKET")
    if production_bucket and production_bucket == bucket:
        raise AssertionError("joint-test MinIO bucket must differ from the production bucket")
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


def _key(content: bytes, *, owner: str, source_id: str) -> str:
    digest = hashlib.sha256(content).hexdigest()
    owner_scope = hashlib.sha256(owner.encode()).hexdigest()
    return f"{owner_scope}/{source_id}/{digest[:2]}/{digest}"


def _upgrade_schema(database_url: str, schema: str) -> None:
    child_env = os.environ.copy()
    child_env["ATHENA_TEST_DATABASE_URL"] = database_url
    child_env["KNOWLEDGE_TEST_ALEMBIC_INI"] = str(ALEMBIC_INI)
    child_env["KNOWLEDGE_TEST_SCHEMA"] = schema
    child_code = """
import os
from alembic import command
from alembic.config import Config
config = Config(os.environ['KNOWLEDGE_TEST_ALEMBIC_INI'])
config.set_main_option(
    'sqlalchemy.url',
    os.environ['ATHENA_TEST_DATABASE_URL'].replace('%', '%%'),
)
config.attributes['connect_args'] = {
    'timeout': 8,
    'command_timeout': 20,
    'server_settings': {
        'search_path': f"{os.environ['KNOWLEDGE_TEST_SCHEMA']},public",
        'statement_timeout': '30000',
        'lock_timeout': '5000',
    },
}
config.attributes['version_table_schema'] = os.environ['KNOWLEDGE_TEST_SCHEMA']
command.upgrade(config, 'head')
"""
    result = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=BACKEND_DIR,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, "joint-test Alembic migration subprocess failed"


@asynccontextmanager
async def _isolated_postgres(
    database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    schema = f"athena_knowledge_joint_{token_hex(8)}"
    admin = create_async_engine(database_url, poolclass=NullPool, connect_args=DB_CONNECT_ARGS)
    engine: AsyncEngine = create_async_engine(
        database_url,
        poolclass=NullPool,
        connect_args={
            **DB_CONNECT_ARGS,
            "server_settings": {
                "search_path": f"{schema},public",
                "statement_timeout": "30000",
                "lock_timeout": "5000",
            },
        },
    )
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await asyncio.to_thread(_upgrade_schema, database_url, schema)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


async def _seed_base(
    sessions: async_sessionmaker[AsyncSession],
    repository: KnowledgeRepository,
    *,
    owner: str,
    base_id: str,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    async with sessions() as session, session.begin():
        session.add(
            UserModel(
                id=owner,
                email=f"{owner}@joint.example",
                password_hash="test-hash",
                display_name=None,
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
    await repository.create_base(KnowledgeBase(base_id, owner, base_id, None, now, now))


async def _cleanup_store(store: MinioOriginalStore, keys: set[str]) -> None:
    for key in sorted(keys):
        try:
            await store.delete(key)
        except OriginalStorageError:
            pass
    await store.aclose()


async def _publish_ingestion(repository: KnowledgeRepository) -> None:
    job = await repository.claim_job(lease_seconds=120)
    assert job is not None
    assert job.kind == "ingestion"
    published = await repository.publish_job(
        job,
        chunks=[("background text", {"kind": "text"}, 2, "background text", [0.01] * 1024)],
        warnings=[],
    )
    assert published


def test_joint_pg_minio_ambiguous_commit_keeps_object_and_replays_idempotently() -> None:
    database_url = DATABASE_URL
    if database_url is None or not _MINIO_CONFIGURED:
        pytest.skip("joint PostgreSQL and MinIO environment is not configured")

    async def scenario() -> None:
        owner = "joint_commit_owner"
        base_id = "joint_commit_base"
        store = MinioOriginalStore(_config())
        created: set[str] = set()
        recording: _RecordingStore | None = None
        try:
            await store.start()
            async with _isolated_postgres(database_url) as sessions:
                repository = KnowledgeRepository(sessions)
                await _seed_base(sessions, repository, owner=owner, base_id=base_id)
                recording = _RecordingStore(store)
                ambiguous_repository = _CommitThenRaiseRepository(repository)
                service = KnowledgeService(
                    cast(KnowledgeRepositoryPort, ambiguous_repository),
                    _WhitespaceTokenizer(),
                    originals=recording,
                    profile_id="profile-v1",
                )
                content = f"ambiguous commit {token_hex(8)}".encode()
                with pytest.raises(ConnectionError, match="lost response"):
                    await service.create_file_document(
                        owner,
                        base_id,
                        "ambiguous",
                        "ambiguous.txt",
                        content,
                        "ambiguous-request",
                    )

                assert ambiguous_repository.committed_result is not None
                assert ambiguous_repository.storage_key is not None
                created.add(ambiguous_repository.storage_key)
                assert recording.put_keys == [ambiguous_repository.storage_key]
                assert recording.delete_keys == []
                assert await store.get(ambiguous_repository.storage_key) == content
                async with sessions() as session:
                    assert (
                        await session.scalar(
                            text(
                                "SELECT count(*) FROM knowledge_upload_reservations "
                                "WHERE owner_id = :owner"
                            ),
                            {"owner": owner},
                        )
                        == 0
                    )
                    assert (
                        await session.scalar(
                            text("SELECT count(*) FROM knowledge_sources WHERE owner_id = :owner"),
                            {"owner": owner},
                        )
                        == 1
                    )

                replay_service = KnowledgeService(
                    cast(KnowledgeRepositoryPort, repository),
                    _WhitespaceTokenizer(),
                    originals=recording,
                    profile_id="profile-v1",
                )
                replay = await replay_service.create_file_document(
                    owner,
                    base_id,
                    "ambiguous",
                    "ambiguous.txt",
                    content,
                    "ambiguous-request",
                )
                assert replay == ambiguous_repository.committed_result
                assert recording.put_keys == [ambiguous_repository.storage_key]
                assert await store.get(ambiguous_repository.storage_key) == content
        finally:
            await _cleanup_store(
                store,
                created | (set(recording.put_keys) if recording is not None else set()),
            )

    asyncio.run(scenario())


def test_joint_pg_minio_cleanup_survives_transient_delete_failure() -> None:
    database_url = DATABASE_URL
    if database_url is None or not _MINIO_CONFIGURED:
        pytest.skip("joint PostgreSQL and MinIO environment is not configured")

    async def scenario() -> None:
        owner = "joint_cleanup_owner"
        base_id = "joint_cleanup_base"
        store = MinioOriginalStore(_config())
        created: set[str] = set()
        recording: _RecordingStore | None = None
        try:
            await store.start()
            async with _isolated_postgres(database_url) as sessions:
                repository = KnowledgeRepository(sessions)
                await _seed_base(sessions, repository, owner=owner, base_id=base_id)
                recording = _RecordingStore(store)
                service = KnowledgeService(
                    cast(KnowledgeRepositoryPort, repository),
                    _WhitespaceTokenizer(),
                    originals=recording,
                    profile_id="profile-v1",
                )
                content = f"cleanup retry {token_hex(8)}".encode()
                document, version_id = await service.create_file_document(
                    owner,
                    base_id,
                    "cleanup",
                    "cleanup.txt",
                    content,
                    "cleanup-request",
                )
                source = await repository.get_source(owner, base_id, document.id, version_id)
                assert source is not None and source.storage_key is not None
                storage_key = source.storage_key
                created.add(storage_key)
                await _publish_ingestion(repository)
                deleted = await repository.delete_document(owner, base_id, document.id)
                assert deleted is not None

                failing_store = _FailOnceDeleteStore(store)
                worker = KnowledgeWorker(
                    repository,
                    cast(DocumentParser, _UnusedParser()),
                    failing_store,
                    cast(EmbeddingPort, _UnusedEmbedding()),
                    cast(ChineseTokenizer, _WhitespaceTokenizer()),
                    cast(TokenCounterPort, _WhitespaceCounter()),
                    isolate_parsing=False,
                )
                assert await worker.run_once()
                assert await store.get(storage_key) == content
                async with sessions() as session:
                    job = await session.scalar(
                        select(KnowledgeJobModel)
                        .where(KnowledgeJobModel.kind == "cleanup")
                        .order_by(KnowledgeJobModel.created_at.desc())
                        .limit(1)
                    )
                    assert job is not None
                    assert job.state == "retry"
                    assert job.attempt == 1
                    await session.execute(
                        update(KnowledgeJobModel)
                        .where(KnowledgeJobModel.id == job.id)
                        .values(next_attempt_at=func.clock_timestamp())
                    )
                    await session.commit()

                assert await worker.run_once()
                with pytest.raises(OriginalStorageNotFoundError):
                    await store.get(storage_key)
                async with sessions() as session:
                    job_state = await session.scalar(
                        select(KnowledgeJobModel.state)
                        .where(KnowledgeJobModel.kind == "cleanup")
                        .order_by(KnowledgeJobModel.created_at.desc())
                        .limit(1)
                    )
                    assert job_state == "done"
                    assert (
                        await session.scalar(
                            select(func.count())
                            .select_from(KnowledgeSourceModel)
                            .where(KnowledgeSourceModel.storage_key == storage_key)
                        )
                        == 0
                    )
        finally:
            await _cleanup_store(
                store,
                created | (set(recording.put_keys) if recording is not None else set()),
            )

    asyncio.run(scenario())


def test_joint_pg_minio_orphan_scan_preserves_source_and_reservation_references() -> None:
    database_url = DATABASE_URL
    if database_url is None or not _MINIO_CONFIGURED:
        pytest.skip("joint PostgreSQL and MinIO environment is not configured")

    async def scenario() -> None:
        owner = "joint_orphan_owner"
        base_id = "joint_orphan_base"
        store = MinioOriginalStore(_config())
        created: set[str] = set()
        recording: _RecordingStore | None = None
        try:
            await store.start()
            async with _isolated_postgres(database_url) as sessions:
                repository = KnowledgeRepository(sessions)
                await _seed_base(sessions, repository, owner=owner, base_id=base_id)
                recording = _RecordingStore(store)
                service = KnowledgeService(
                    cast(KnowledgeRepositoryPort, repository),
                    _WhitespaceTokenizer(),
                    originals=recording,
                    profile_id="profile-v1",
                )
                source_content = f"referenced source {token_hex(8)}".encode()
                document, version_id = await service.create_file_document(
                    owner,
                    base_id,
                    "referenced source",
                    "source.txt",
                    source_content,
                    "source-request",
                )
                source = await repository.get_source(owner, base_id, document.id, version_id)
                assert source is not None and source.storage_key is not None
                source_key = source.storage_key
                created.add(source_key)
                await _publish_ingestion(repository)

                reservation_content = f"referenced reservation {token_hex(8)}".encode()
                reservation_key = _key(
                    reservation_content,
                    owner=owner,
                    source_id="ksrc_reserved",
                )
                await store.put(reservation_key, reservation_content)
                created.add(reservation_key)
                await repository.reserve_file_upload(
                    owner_id=owner,
                    operation="create",
                    request_key="reserved-request",
                    request_hash="reserved-request-hash",
                    base_id=base_id,
                    document_id="kdoc_reserved",
                    size_bytes=len(reservation_content),
                    reservation_token="kres_reserved",
                    storage_key=reservation_key,
                    source_id="ksrc_reserved",
                    version_id="kver_reserved",
                    job_id="kjob_reserved",
                    at=datetime(2026, 1, 1, tzinfo=UTC),
                )

                orphan_content = f"orphan {token_hex(8)}".encode()
                orphan_key = _key(orphan_content, owner=owner, source_id="ksrc_orphan")
                await store.put(orphan_key, orphan_content)
                created.add(orphan_key)
                injected = _InjectedCandidateStore(
                    store,
                    (source_key, reservation_key, orphan_key),
                )
                worker = KnowledgeWorker(
                    repository,
                    cast(DocumentParser, _UnusedParser()),
                    injected,
                    cast(EmbeddingPort, _UnusedEmbedding()),
                    cast(ChineseTokenizer, _WhitespaceTokenizer()),
                    cast(TokenCounterPort, _WhitespaceCounter()),
                    isolate_parsing=False,
                )
                worker._sweep_counter = 99
                assert not await worker.run_once()
                assert injected.delete_keys == [orphan_key]
                assert await store.get(source_key) == source_content
                assert await store.get(reservation_key) == reservation_content
                with pytest.raises(OriginalStorageNotFoundError):
                    await store.get(orphan_key)
        finally:
            await _cleanup_store(
                store,
                created | (set(recording.put_keys) if recording is not None else set()),
            )

    asyncio.run(scenario())
