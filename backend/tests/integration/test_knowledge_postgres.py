"""Real PostgreSQL migration, owner isolation, idempotency, and hybrid search tests."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex
from threading import Thread
from typing import Any, cast

import pytest
from sqlalchemy import event, insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.auth.model import UserModel
from adapter.knowledge.model import KnowledgeDocumentModel
from adapter.knowledge.repository import KnowledgeRepository
from app.knowledge.service import KnowledgeService
from domain.knowledge.model import Document, KnowledgeBase
from domain.knowledge.ports import ClaimedKnowledgeJob, KnowledgeRepositoryPort
from infrastructure.knowledge.storage import LocalOriginalStore

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
DB_CONNECT_ARGS = {"timeout": 8, "command_timeout": 12}
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="test PostgreSQL URL is not configured"),
]


class _UploadTokenizer:
    def tokenize(self, value: str) -> tuple[str, ...]:
        return tuple(value.split())


class _CommitThenRaiseRepository:
    """Simulate a lost response after the PostgreSQL commit completed."""

    def __init__(self, repository: KnowledgeRepository) -> None:
        self._repository = repository
        self.storage_key: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repository, name)

    async def create_file_document(self, **kwargs: Any) -> tuple[Document, str]:
        await self._repository.create_file_document(**kwargs)
        self.storage_key = str(kwargs["storage_key"])
        raise ConnectionError("simulated response loss after commit")


async def _claim_job_target(
    repository: KnowledgeRepository, target_id: str
) -> ClaimedKnowledgeJob:
    """Claim a target task while completing other known ingestion jobs in queue order."""
    for _ in range(20):
        job = await repository.claim_job(lease_seconds=120)
        assert job is not None, f"queued task {target_id} was not claimable"
        if job.target_id == target_id:
            return job
        assert job.kind == "ingestion", f"unexpected cleanup task before {target_id}"
        content = job.source_text or f"completed unrelated task {job.title}"
        assert await repository.publish_job(
            job,
            chunks=[(content, {"kind": "text"}, 1, content, [0.01] * 1024)],
            warnings=[],
        )
    raise AssertionError(f"queued task {target_id} was not reached")


def _upgrade_schema(database_url: str, schema: str) -> None:
    """Run Alembic in an isolated process so logging config cannot affect pytest."""
    child_env = os.environ.copy()
    child_env["ATHENA_TEST_DATABASE_URL"] = database_url
    child_env["KNOWLEDGE_TEST_ALEMBIC_INI"] = str(ALEMBIC_INI)
    child_env["KNOWLEDGE_TEST_SCHEMA"] = schema
    child_code = """
import os
from alembic import command
from alembic.config import Config
config = Config(os.environ['KNOWLEDGE_TEST_ALEMBIC_INI'])
config.set_main_option('sqlalchemy.url', os.environ['ATHENA_TEST_DATABASE_URL'].replace('%', '%%'))
config.attributes['connect_args'] = {
    'timeout': 8,
    'command_timeout': 12,
    'server_settings': {
        'search_path': f\"{os.environ['KNOWLEDGE_TEST_SCHEMA']},public\",
        'statement_timeout': '15000',
        'lock_timeout': '5000',
    },
}
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
    assert result.returncode == 0, "knowledge Alembic migration subprocess failed"


def test_knowledge_migration_and_repository_use_owner_scoped_postgres_search() -> None:
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("test PostgreSQL URL is not configured")

    async def scenario() -> None:
        schema = f"athena_knowledge_{token_hex(8)}"
        admin = create_async_engine(database_url, poolclass=NullPool, connect_args=DB_CONNECT_ARGS)
        engine = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args={
                **DB_CONNECT_ARGS,
                "server_settings": {
                    "search_path": f"{schema},public",
                    "statement_timeout": "15000",
                    "lock_timeout": "5000",
                },
            },
        )
        try:
            async with admin.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            _upgrade_schema(database_url, schema)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            repository = KnowledgeRepository(session_factory)
            now = datetime(2026, 1, 1, tzinfo=UTC)
            async with session_factory() as session, session.begin():
                session.add_all(
                    UserModel(
                        id=user,
                        email=f"{user}@knowledge.example",
                        password_hash="test-hash",
                        display_name=None,
                        status="active",
                        created_at=now,
                        updated_at=now,
                    )
                    for user in (
                        "kb_owner_a",
                        "kb_owner_b",
                        "quota_owner",
                        "quota_bytes_owner",
                        "upload_owner",
                    )
                )
            for owner, base_id in (("kb_owner_a", "kb_a"), ("kb_owner_b", "kb_b")):
                await repository.create_base(KnowledgeBase(base_id, owner, base_id, None, now, now))

            await repository.create_base(
                KnowledgeBase("upload_kb", "upload_owner", "uploads", None, now, now)
            )
            interleaved_content = b"committed during referenced keys snapshot"
            interleaved_hash = hashlib.sha256(interleaved_content).hexdigest()
            interleaved_source_id = "snapshot_source"
            interleaved_reservation = await repository.reserve_file_upload(
                owner_id="upload_owner",
                operation="create",
                request_key="snapshot-race-key",
                request_hash="snapshot-race-hash",
                base_id="upload_kb",
                document_id="snapshot_doc",
                size_bytes=len(interleaved_content),
                reservation_token="snapshot-race-token",
                storage_key=(
                    hashlib.sha256(b"upload_owner").hexdigest()
                    + f"/{interleaved_source_id}/{interleaved_hash[:2]}/{interleaved_hash}"
                ),
                source_id=interleaved_source_id,
                version_id="snapshot_version",
                job_id="snapshot_job",
                at=now,
            )
            assert interleaved_reservation.storage_key is not None
            assert interleaved_reservation.reservation_token is not None
            assert interleaved_reservation.source_id is not None
            assert interleaved_reservation.job_id is not None
            interleaved_done = False
            interleaved_errors: list[BaseException] = []

            def finalize_before_referenced_query(
                connection: Any,
                cursor: Any,
                statement: str,
                parameters: Any,
                context: Any,
                executemany: bool,
            ) -> None:
                del connection, cursor, parameters, context, executemany
                nonlocal interleaved_done
                if interleaved_done or (
                    "from knowledge_upload_reservations" not in statement.lower()
                ):
                    return
                interleaved_done = True

                async def finalize() -> None:
                    thread_engine = create_async_engine(
                        database_url,
                        poolclass=NullPool,
                        connect_args={
                            **DB_CONNECT_ARGS,
                            "server_settings": {
                                "search_path": f"{schema},public",
                                "statement_timeout": "15000",
                                "lock_timeout": "5000",
                            },
                        },
                    )
                    try:
                        thread_repository = KnowledgeRepository(
                            async_sessionmaker(thread_engine, expire_on_commit=False)
                        )
                        await thread_repository.create_file_document(
                            owner_id="upload_owner",
                            base_id="upload_kb",
                            title="snapshot race",
                            filename="snapshot.txt",
                            media_type="text/plain",
                            storage_key=interleaved_reservation.storage_key or "",
                            size_bytes=len(interleaved_content),
                            sha256=interleaved_hash,
                            request_key="snapshot-race-key",
                            request_hash="snapshot-race-hash",
                            document_id="snapshot_doc",
                            version_id="snapshot_version",
                            source_id=interleaved_reservation.source_id or "",
                            job_id=interleaved_reservation.job_id or "",
                            reservation_token=interleaved_reservation.reservation_token or "",
                            profile_id="profile-v1",
                            at=now,
                        )
                    finally:
                        await thread_engine.dispose()

                def run_finalize() -> None:
                    try:
                        asyncio.run(finalize())
                    except BaseException as error:
                        interleaved_errors.append(error)

                finalizer_thread = Thread(target=run_finalize, daemon=True)
                finalizer_thread.start()
                finalizer_thread.join(timeout=30)
                if finalizer_thread.is_alive():
                    interleaved_errors.append(TimeoutError("reservation finalize timed out"))

            event.listen(
                engine.sync_engine,
                "before_cursor_execute",
                finalize_before_referenced_query,
            )
            try:
                interleaved_references = await repository.list_referenced_storage_keys()
            finally:
                event.remove(
                    engine.sync_engine,
                    "before_cursor_execute",
                    finalize_before_referenced_query,
                )
            assert interleaved_done
            assert not interleaved_errors, f"interleaved finalize failed: {interleaved_errors!r}"
            assert interleaved_reservation.storage_key in interleaved_references

            with tempfile.TemporaryDirectory(prefix="knowledge-upload-race-") as storage_root:
                originals = LocalOriginalStore(storage_root)
                upload_service = KnowledgeService(
                    cast(KnowledgeRepositoryPort, repository),
                    _UploadTokenizer(),
                    originals=originals,
                )
                upload_content = b"same hash uploaded by two concurrent requests"
                upload_results = await asyncio.gather(
                    upload_service.create_file_document(
                        "upload_owner",
                        "upload_kb",
                        "same request",
                        "report.txt",
                        upload_content,
                        "same-key",
                    ),
                    upload_service.create_file_document(
                        "upload_owner",
                        "upload_kb",
                        "same request",
                        "report.txt",
                        upload_content,
                        "same-key",
                    ),
                )
                assert upload_results[0] == upload_results[1]
                assert len([path for path in Path(storage_root).rglob("*") if path.is_file()]) == 1
                winning_document, winning_version = upload_results[0]
                winning_source = await repository.get_source(
                    "upload_owner", "upload_kb", winning_document.id, winning_version
                )
                assert winning_source is not None
                assert winning_source.storage_key is not None
                assert await originals.get(winning_source.storage_key) == upload_content

                canonical_service = KnowledgeService(
                    cast(KnowledgeRepositoryPort, repository),
                    _UploadTokenizer(),
                    originals=originals,
                )
                await repository.create_base(
                    KnowledgeBase("kb_a_other", "kb_owner_a", "other", None, now, now)
                )
                canonical_text = await canonical_service.create_text_document(
                    "kb_owner_a", "kb_a", "canonical title", "canonical body", "canonical-key"
                )
                assert await canonical_service.create_text_document(
                    "kb_owner_a", "kb_a", "canonical title", "canonical body", "canonical-key"
                ) == canonical_text
                with pytest.raises(ValueError, match="idempotency_conflict"):
                    await canonical_service.create_text_document(
                        "kb_owner_a",
                        "kb_a",
                        "different title",
                        "canonical body",
                        "canonical-key",
                    )
                with pytest.raises(ValueError, match="idempotency_conflict"):
                    await canonical_service.create_text_document(
                        "kb_owner_a",
                        "kb_a_other",
                        "canonical title",
                        "canonical body",
                        "canonical-key",
                    )
                with pytest.raises(ValueError, match="idempotency_conflict"):
                    files_before_source_kind_conflict = len(
                        [path for path in Path(storage_root).rglob("*") if path.is_file()]
                    )
                    await canonical_service.create_file_document(
                        "kb_owner_a",
                        "kb_a",
                        "canonical title",
                        "canonical.txt",
                        b"canonical body",
                        "canonical-key",
                    )
                assert len(
                    [path for path in Path(storage_root).rglob("*") if path.is_file()]
                ) == files_before_source_kind_conflict
                files_before_first_canonical_file = len(
                    [path for path in Path(storage_root).rglob("*") if path.is_file()]
                )
                canonical_file = await canonical_service.create_file_document(
                    "kb_owner_a",
                    "kb_a",
                    "file title",
                    "canonical.txt",
                    b"same file content",
                    "canonical-file-key",
                )
                files_after_first_canonical_file = len(
                    [path for path in Path(storage_root).rglob("*") if path.is_file()]
                )
                assert files_after_first_canonical_file == files_before_first_canonical_file + 1
                assert await canonical_service.create_file_document(
                    "kb_owner_a",
                    "kb_a",
                    "file title",
                    "canonical.txt",
                    b"same file content",
                    "canonical-file-key",
                ) == canonical_file
                assert len(
                    [path for path in Path(storage_root).rglob("*") if path.is_file()]
                ) == files_after_first_canonical_file
                with pytest.raises(ValueError, match="idempotency_conflict"):
                    await canonical_service.create_file_document(
                        "kb_owner_a",
                        "kb_a",
                        "file title",
                        "renamed.txt",
                        b"same file content",
                        "canonical-file-key",
                    )
                assert len(
                    [path for path in Path(storage_root).rglob("*") if path.is_file()]
                ) == files_after_first_canonical_file
                ambiguous_commit = _CommitThenRaiseRepository(repository)
                uncertain_service = KnowledgeService(
                    cast(KnowledgeRepositoryPort, ambiguous_commit),
                    _UploadTokenizer(),
                    originals=originals,
                )
                with pytest.raises(ConnectionError, match="simulated response loss"):
                    await uncertain_service.create_file_document(
                        "upload_owner",
                        "upload_kb",
                        "committed despite lost response",
                        "uncertain.txt",
                        b"committed before response lost",
                        "ambiguous-commit-key",
                    )
                assert ambiguous_commit.storage_key is not None
                assert await originals.get(ambiguous_commit.storage_key) == (
                    b"committed before response lost"
                )
                async with session_factory() as session:
                    committed_source_count = await session.scalar(
                        text(
                            "SELECT count(*) FROM knowledge_sources "
                            "WHERE owner_id = 'upload_owner' AND storage_key = :storage_key"
                        ),
                        {"storage_key": ambiguous_commit.storage_key},
                    )
                assert committed_source_count == 1

                async def concurrent_file_create() -> tuple[Document, str]:
                    return await canonical_service.create_file_document(
                        "upload_owner",
                        "upload_kb",
                        "idempotent file",
                        "same.txt",
                        upload_content,
                        "same-file-request",
                    )

                concurrent_files = await asyncio.gather(
                    concurrent_file_create(), concurrent_file_create()
                )
                assert concurrent_files[0] == concurrent_files[1]
                file_request_race = await asyncio.gather(
                    concurrent_file_create(),
                    canonical_service.create_file_document(
                        "upload_owner",
                        "upload_kb",
                        "conflicting file",
                        "same.txt",
                        upload_content,
                        "same-file-request",
                    ),
                    return_exceptions=True,
                )
                assert sum(
                    not isinstance(result, Exception) for result in file_request_race
                ) == 1
                file_conflicts = [
                    result for result in file_request_race if isinstance(result, ValueError)
                ]
                assert len(file_conflicts) == 1
                assert str(file_conflicts[0]) == "idempotency_conflict"
                async with session_factory() as session:
                    idempotent_file_count = await session.scalar(
                        text(
                            "SELECT count(*) FROM knowledge_requests "
                            "WHERE request_key = 'same-file-request'"
                        )
                    )
                assert idempotent_file_count == 1

                orphan_content = b"old unreferenced upload"
                orphan_hash = hashlib.sha256(orphan_content).hexdigest()
                owner_hash = hashlib.sha256(b"upload_owner").hexdigest()
                orphan_key = f"{owner_hash}/orphan/{orphan_hash[:2]}/{orphan_hash}"
                await originals.put(orphan_key, orphan_content)
                stale_time = time.time() - 90_000
                os.utime(Path(storage_root) / orphan_key, (stale_time, stale_time))
                assert winning_source.storage_key is not None
                swept = await originals.sweep_orphans(
                    {winning_source.storage_key}, older_than_seconds=86_400
                )
                assert swept == 1
                assert await originals.get(winning_source.storage_key) == upload_content
                assert not (Path(storage_root) / orphan_key).exists()

            async def concurrent_create(suffix: str) -> tuple[Document, str]:
                return await repository.create_text_document(
                    owner_id="kb_owner_a",
                    base_id="kb_a",
                    title="alpha",
                    text="PostgreSQL tenant alpha",
                    request_key="create-a",
                    request_hash="hash-alpha",
                    content_hash="hash-alpha-content",
                    document_id=f"doc_a_{suffix}",
                    version_id=f"ver_a_{suffix}",
                    job_id=f"job_a_{suffix}",
                    profile_id="profile-v1",
                    at=now,
                )

            concurrent_creates = await asyncio.gather(
                concurrent_create("first"), concurrent_create("second")
            )
            assert concurrent_creates[0] == concurrent_creates[1]
            doc_a, ver_a = concurrent_creates[0]

            create_race = await asyncio.gather(
                concurrent_create("same-request"),
                repository.create_text_document(
                    owner_id="kb_owner_a",
                    base_id="kb_a",
                    title="conflicting create",
                    text="different payload",
                    request_key="create-a",
                    request_hash="conflicting-hash",
                    content_hash="conflicting-content-hash",
                    document_id="create-conflict-doc",
                    version_id="create-conflict-version",
                    job_id="create-conflict-job",
                    profile_id="profile-v1",
                    at=now,
                ),
                return_exceptions=True,
            )
            assert sum(not isinstance(result, Exception) for result in create_race) == 1
            create_conflicts = [result for result in create_race if isinstance(result, ValueError)]
            assert len(create_conflicts) == 1
            assert str(create_conflicts[0]) == "idempotency_conflict"
            replay, replay_version = await repository.create_text_document(
                owner_id="kb_owner_a",
                base_id="kb_a",
                title="alpha",
                text="PostgreSQL tenant alpha",
                request_key="create-a",
                request_hash="hash-alpha",
                content_hash="hash-alpha-content",
                document_id="ignored-doc",
                version_id="ignored-version",
                job_id="ignored-job",
                profile_id="profile-v1",
                at=now,
            )
            assert (replay.id, replay_version) == (doc_a.id, ver_a)
            with pytest.raises(ValueError, match="idempotency_conflict"):
                await repository.create_text_document(
                    owner_id="kb_owner_a",
                    base_id="kb_a",
                    title="alpha",
                    text="changed",
                    request_key="create-a",
                    request_hash="different-hash",
                    content_hash="different-content-hash",
                    document_id="new-doc",
                    version_id="new-version",
                    job_id="new-job",
                    profile_id="profile-v1",
                    at=now,
                )
            await repository.create_text_document(
                owner_id="kb_owner_b",
                base_id="kb_b",
                title="beta",
                text="PostgreSQL tenant beta",
                request_key="create-b",
                request_hash="hash-beta",
                content_hash="hash-beta-content",
                document_id="doc_b",
                version_id="ver_b",
                job_id="job_b",
                profile_id="profile-v1",
                at=now,
            )
            for expected_version in (ver_a, "ver_b"):
                job = await _claim_job_target(repository, expected_version)
                assert await repository.publish_job(
                    job,
                    chunks=[
                        (
                            f"{job.owner_id} PostgreSQL tenant token",
                            {"kind": "text"},
                            4,
                            f"{job.owner_id} PostgreSQL tenant token",
                            [0.1] * 1024,
                        )
                    ],
                    warnings=[],
                )

            own_hits = await repository.search(
                "kb_owner_a", ["kb_a"], "PostgreSQL", ("PostgreSQL",), 10, [0.1] * 1024
            )
            assert len(own_hits.hits) == 1
            assert own_hits.hits[0].document_id == doc_a.id
            assert set(own_hits.hits[0].channels) == {"keyword", "semantic"}
            with pytest.raises(LookupError, match="knowledge_base_not_found"):
                await repository.search("kb_owner_a", ["kb_b"], "PostgreSQL", ("PostgreSQL",), 10)
            await repository.validate_search_scope("kb_owner_a", ["kb_a"])
            with pytest.raises(LookupError, match="knowledge_base_not_found"):
                await repository.validate_search_scope("kb_owner_a", ["kb_b"])
            keyword_hits = await repository.search_keyword(
                "kb_owner_a", ["kb_a"], ("PostgreSQL",), 40
            )
            semantic_hits = await repository.search_semantic(
                "kb_owner_a", ["kb_a"], [0.1] * 1024, 40, "profile-v1"
            )
            assert len(keyword_hits) == 1
            assert keyword_hits[0].document_id == doc_a.id
            assert keyword_hits[0].channels == ("keyword",)
            assert len(semantic_hits) == 1
            assert semantic_hits[0].document_id == doc_a.id
            assert semantic_hits[0].channels == ("semantic",)
            incompatible_hits = await repository.search_semantic(
                "kb_owner_a", ["kb_a"], [0.1] * 1024, 40, "incompatible-profile"
            )
            assert incompatible_hits == ()
            stale_hits = await repository.search_keyword(
                "kb_owner_a", ["kb_a"], ("PostgreSQL",), 40
            )

            async def concurrent_replace(suffix: str) -> tuple[Document, str]:
                return await repository.replace_text_document(
                    owner_id="kb_owner_a",
                    base_id="kb_a",
                    document_id=doc_a.id,
                    text="alpha failed replacement",
                    request_key="replace-failed",
                    request_hash="failed-replacement-hash",
                    content_hash="failed-replacement-content-hash",
                    version_id=f"failed_ver_{suffix}",
                    job_id=f"failed_job_{suffix}",
                    profile_id="profile-v1",
                    at=now + timedelta(seconds=1),
                )

            concurrent_replacements = await asyncio.gather(
                concurrent_replace("first"), concurrent_replace("second")
            )
            assert concurrent_replacements[0] == concurrent_replacements[1]
            failed_update, failed_version = concurrent_replacements[0]
            replace_race = await asyncio.gather(
                concurrent_replace("same-request"),
                repository.replace_text_document(
                    owner_id="kb_owner_a",
                    base_id="kb_a",
                    document_id=doc_a.id,
                    text="different replacement payload",
                    request_key="replace-failed",
                    request_hash="conflicting-replacement-hash",
                    content_hash="conflicting-replacement-content-hash",
                    version_id="replace-conflict-version",
                    job_id="replace-conflict-job",
                    profile_id="profile-v1",
                    at=now + timedelta(seconds=1),
                ),
                return_exceptions=True,
            )
            assert sum(not isinstance(result, Exception) for result in replace_race) == 1
            replace_conflicts = [
                result for result in replace_race if isinstance(result, ValueError)
            ]
            assert len(replace_conflicts) == 1
            assert str(replace_conflicts[0]) == "idempotency_conflict"
            assert failed_update.active_version_id == ver_a
            assert failed_update.pending_version_id == failed_version
            failed_job = await _claim_job_target(repository, failed_version)
            await repository.fail_job(failed_job, error_code="parser_failed", retryable=False)
            after_failure = await repository.get_document("kb_owner_a", "kb_a", doc_a.id)
            assert after_failure is not None
            assert after_failure.active_version_id == ver_a
            assert after_failure.pending_version_id is None
            after_failure_hits = await repository.search(
                "kb_owner_a", ["kb_a"], "PostgreSQL", ("PostgreSQL",), 10
            )
            assert after_failure_hits.hits[0].version_id == ver_a

            replacement, replacement_version = await repository.replace_text_document(
                owner_id="kb_owner_a",
                base_id="kb_a",
                document_id=doc_a.id,
                text="replacement content beta",
                request_key="replace-success",
                request_hash="replacement-hash",
                content_hash="replacement-content-hash",
                version_id="replacement_ver",
                job_id="replacement_job",
                profile_id="profile-v1",
                at=now + timedelta(seconds=2),
            )
            assert replacement.active_version_id == ver_a
            replacement_job = await _claim_job_target(repository, replacement_version)
            await repository.fail_job(
                replacement_job, error_code="transient_fixture", retryable=False
            )
            retried = await repository.retry_version(
                "kb_owner_a",
                "kb_a",
                doc_a.id,
                replacement_version,
                "retry_job",
                now + timedelta(seconds=3),
            )
            assert retried is not None and retried.state.value == "queued"
            retry_job = await _claim_job_target(repository, replacement_version)
            assert retry_job.fencing_token == replacement_job.fencing_token + 1
            assert await repository.publish_job(
                retry_job,
                chunks=[
                    (
                        "replacement content beta",
                        {"kind": "text"},
                        3,
                        "replacement content beta",
                        [0.2] * 1024,
                    )
                ],
                warnings=[],
            )
            with pytest.raises(ValueError, match="version_superseded"):
                await repository.retry_version(
                    "kb_owner_a",
                    "kb_a",
                    doc_a.id,
                    failed_version,
                    "old_retry_job",
                    now + timedelta(seconds=4),
                )
            after_success_hits = await repository.search(
                "kb_owner_a", ["kb_a"], "replacement", ("replacement",), 10
            )
            assert after_success_hits.hits
            assert all(hit.version_id == replacement_version for hit in after_success_hits.hits)
            assert all("replacement content" in hit.text for hit in after_success_hits.hits)
            assert await repository.filter_active_hits("kb_owner_a", tuple(stale_hits)) == ()
            assert await repository.filter_active_hits("kb_owner_a", tuple(own_hits.hits)) == ()
            source = await repository.get_source(
                "kb_owner_a", "kb_a", doc_a.id, replacement_version
            )
            assert source is not None and source.text == "replacement content beta"
            assert (
                await repository.get_source("kb_owner_b", "kb_a", doc_a.id, replacement_version)
                is None
            )
            chunk_id = after_success_hits.hits[0].chunk_id
            assert await repository.get_chunk("kb_owner_a", "kb_a", chunk_id) is not None
            assert await repository.get_chunk("kb_owner_b", "kb_a", chunk_id) is None

            renamed_replay_service = KnowledgeService(
                cast(KnowledgeRepositoryPort, repository), _UploadTokenizer()
            )
            text_replace = await renamed_replay_service.replace_text_document(
                "kb_owner_a",
                "kb_a",
                doc_a.id,
                "replacement survives a title change",
                "rename-text-replay",
            )
            await renamed_replay_service.rename_document(
                "kb_owner_a", "kb_a", doc_a.id, "renamed text document"
            )
            text_replace_replay = await renamed_replay_service.replace_text_document(
                "kb_owner_a",
                "kb_a",
                doc_a.id,
                "replacement survives a title change",
                "rename-text-replay",
            )
            assert (text_replace_replay[0].id, text_replace_replay[1]) == (
                text_replace[0].id,
                text_replace[1],
            )
            text_replace_job = await _claim_job_target(repository, text_replace[1])
            assert await repository.publish_job(
                text_replace_job,
                chunks=[
                    (
                        "replacement survives a title change",
                        {"kind": "text"},
                        5,
                        "replacement survives a title change",
                        [0.23] * 1024,
                    )
                ],
                warnings=[],
            )

            with tempfile.TemporaryDirectory(prefix="knowledge-replace-replay-") as storage_root:
                replacement_store = LocalOriginalStore(storage_root)
                file_replace_service = KnowledgeService(
                    cast(KnowledgeRepositoryPort, repository),
                    _UploadTokenizer(),
                    originals=replacement_store,
                )
                replacement_payload = b"replacement file content after rename"
                file_replace = await file_replace_service.replace_file_document(
                    "kb_owner_a",
                    "kb_a",
                    doc_a.id,
                    "replacement.txt",
                    replacement_payload,
                    "rename-file-replay",
                )
                files_after_first_replace = len(list(Path(storage_root).rglob("*")))
                await file_replace_service.rename_document(
                    "kb_owner_a", "kb_a", doc_a.id, "renamed file document"
                )
                file_replace_replay = await file_replace_service.replace_file_document(
                    "kb_owner_a",
                    "kb_a",
                    doc_a.id,
                    "replacement.txt",
                    replacement_payload,
                    "rename-file-replay",
                )
                assert (file_replace_replay[0].id, file_replace_replay[1]) == (
                    file_replace[0].id,
                    file_replace[1],
                )
                assert len(list(Path(storage_root).rglob("*"))) == files_after_first_replace
                file_replace_job = await _claim_job_target(repository, file_replace[1])
                assert await repository.publish_job(
                    file_replace_job,
                    chunks=[
                        (
                            "replacement file content after rename",
                            {"kind": "text"},
                            5,
                            "replacement file content after rename",
                            [0.27] * 1024,
                        )
                    ],
                    warnings=[],
                )
                file_replace_source = await repository.get_source(
                    "kb_owner_a", "kb_a", doc_a.id, file_replace[1]
                )
                assert file_replace_source is not None
                assert file_replace_source.storage_key is not None

            assert await repository.delete_document("kb_owner_a", "kb_a", doc_a.id) == []
            assert await repository.get_document("kb_owner_a", "kb_a", doc_a.id) is None
            document_cleanup = await _claim_job_target(repository, f"doc:{doc_a.id}")
            assert document_cleanup.kind == "cleanup"
            assert document_cleanup.target_id == f"doc:{doc_a.id}"
            assert document_cleanup.source_keys == (file_replace_source.storage_key,)
            await repository.fail_job(
                document_cleanup, error_code="storage_delete_failed", retryable=True
            )
            async with session_factory() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE knowledge_jobs SET next_attempt_at = now() - interval '1 second' "
                        "WHERE id = :job_id"
                    ),
                    {"job_id": document_cleanup.job_id},
                )
            document_cleanup_retry = await _claim_job_target(
                repository, f"doc:{doc_a.id}"
            )
            assert document_cleanup_retry.target_id == f"doc:{doc_a.id}"
            assert document_cleanup_retry.fencing_token == document_cleanup.fencing_token + 1
            assert not await repository.finish_cleanup_job(document_cleanup)
            assert await repository.finish_cleanup_job(document_cleanup_retry)
            assert (
                await repository.get_source("kb_owner_a", "kb_a", doc_a.id, replacement_version)
                is None
            )
            assert await repository.get_chunk("kb_owner_a", "kb_a", chunk_id) is None
            deleted_hits = await repository.search(
                "kb_owner_a", ["kb_a"], "PostgreSQL", ("PostgreSQL",), 10
            )
            assert deleted_hits.hits == ()
            async with session_factory() as session:
                count = await session.scalar(
                    text("SELECT count(*) FROM knowledge_chunks WHERE document_id = 'doc_b'")
                )
                assert count == 1

            _, race_version = await repository.replace_text_document(
                owner_id="kb_owner_b",
                base_id="kb_b",
                document_id="doc_b",
                text="publish delete race replacement",
                request_key="race-replace",
                request_hash="race-hash",
                content_hash="race-content-hash",
                version_id="race_ver",
                job_id="race_job",
                profile_id="profile-v1",
                at=now + timedelta(seconds=5),
            )
            race_job = await _claim_job_target(repository, race_version)
            published, deletion_keys = await asyncio.gather(
                repository.publish_job(
                    race_job,
                    chunks=[
                        (
                            "publish delete race replacement",
                            {"kind": "text"},
                            4,
                            "publish delete race replacement",
                            [0.3] * 1024,
                        )
                    ],
                    warnings=[],
                ),
                repository.delete_document("kb_owner_b", "kb_b", "doc_b"),
            )
            assert isinstance(published, bool)
            assert deletion_keys == []
            assert await repository.get_document("kb_owner_b", "kb_b", "doc_b") is None
            race_hits = await repository.search(
                "kb_owner_b", ["kb_b"], "replacement", ("replacement",), 10
            )
            assert race_hits.hits == ()
            async with session_factory() as session:
                race_rows_before_cleanup = await session.scalar(
                    text("SELECT count(*) FROM knowledge_chunks WHERE document_id = 'doc_b'")
                )
                assert race_rows_before_cleanup in {1, 2}
            race_cleanup = await _claim_job_target(repository, "doc:doc_b")
            assert await repository.finish_cleanup_job(race_cleanup)
            async with session_factory() as session:
                race_rows = await session.scalar(
                    text("SELECT count(*) FROM knowledge_chunks WHERE document_id = 'doc_b'")
                )
                assert race_rows == 0

            await repository.create_base(
                KnowledgeBase("quota_kb", "quota_owner", "quota", None, now, now)
            )
            async with session_factory() as session, session.begin():
                await session.execute(
                    insert(KnowledgeDocumentModel),
                    [
                        {
                            "id": f"quota_doc_{index}",
                            "owner_id": "quota_owner",
                            "knowledge_base_id": "quota_kb",
                            "title": "existing",
                            "active_version_id": None,
                            "pending_version_id": None,
                            "created_at": now,
                            "updated_at": now,
                            "deleted_time": None,
                        }
                        for index in range(999)
                    ],
                )

            async def import_quota_document(index: int) -> tuple[Document, str]:
                return await repository.create_text_document(
                    owner_id="quota_owner",
                    base_id="quota_kb",
                    title=f"concurrent {index}",
                    text=f"concurrent payload {index}",
                    request_key=f"quota-request-{index}",
                    request_hash=f"quota-hash-{index}",
                    content_hash=f"quota-content-hash-{index}",
                    document_id=f"quota_new_{index}",
                    version_id=f"quota_version_{index}",
                    job_id=f"quota_job_{index}",
                    profile_id="profile-v1",
                    at=now,
                )

            quota_results = await asyncio.gather(
                import_quota_document(1), import_quota_document(2), return_exceptions=True
            )
            assert sum(not isinstance(result, Exception) for result in quota_results) == 1
            quota_errors = [result for result in quota_results if isinstance(result, Exception)]
            assert len(quota_errors) == 1
            assert isinstance(quota_errors[0], ValueError)
            assert str(quota_errors[0]) == "knowledge_document_quota_exceeded"

            await repository.create_base(
                KnowledgeBase("quota_bytes_kb", "quota_bytes_owner", "bytes", None, now, now)
            )
            bytes_reservation = await repository.reserve_file_upload(
                owner_id="quota_bytes_owner",
                operation="create",
                request_key="bytes-create",
                request_hash="bytes-create-hash",
                base_id="quota_bytes_kb",
                document_id="bytes_doc",
                size_bytes=1024 * 1024 * 1024,
                reservation_token="bytes-create-reservation",
                storage_key=("a" * 64) + "/bytes_source/aa/" + ("a" * 64),
                source_id="bytes_source",
                version_id="bytes_version",
                job_id="bytes_job",
                at=now,
            )
            assert bytes_reservation.reservation_token is not None
            assert bytes_reservation.storage_key is not None
            await repository.create_file_document(
                owner_id="quota_bytes_owner",
                base_id="quota_bytes_kb",
                title="large source",
                filename="large.txt",
                media_type="text/plain",
                storage_key=bytes_reservation.storage_key,
                size_bytes=1024 * 1024 * 1024,
                sha256="a" * 64,
                request_key="bytes-create",
                request_hash="bytes-create-hash",
                document_id="bytes_doc",
                version_id="bytes_version",
                source_id="bytes_source",
                job_id="bytes_job",
                reservation_token=bytes_reservation.reservation_token,
                profile_id="profile-v1",
                at=now,
            )
            large_source_job = await _claim_job_target(repository, "bytes_version")
            assert await repository.publish_job(
                large_source_job,
                chunks=[
                    (
                        "large source fixture",
                        {"kind": "text"},
                        3,
                        "large source fixture",
                        [0.31] * 1024,
                    )
                ],
                warnings=[],
            )
            with tempfile.TemporaryDirectory(prefix="knowledge-quota-reject-") as storage_root:
                quota_store = LocalOriginalStore(storage_root)
                quota_service = KnowledgeService(
                    cast(KnowledgeRepositoryPort, repository),
                    _UploadTokenizer(),
                    originals=quota_store,
                )
                with pytest.raises(ValueError, match="knowledge_storage_quota_exceeded"):
                    await quota_service.create_file_document(
                        "quota_bytes_owner",
                        "quota_bytes_kb",
                        "over quota",
                        "small.txt",
                        b"small upload",
                        "quota-create-reject",
                    )
                with pytest.raises(ValueError, match="knowledge_storage_quota_exceeded"):
                    await quota_service.replace_file_document(
                        "quota_bytes_owner",
                        "quota_bytes_kb",
                        "bytes_doc",
                        "small.txt",
                        b"small replacement",
                        "quota-replace-reject",
                    )
                assert list(Path(storage_root).rglob("*")) == []
        finally:
            await engine.dispose()
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin.dispose()

    asyncio.run(scenario())
