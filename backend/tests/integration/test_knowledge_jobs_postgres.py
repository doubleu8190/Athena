"""Real PostgreSQL lease reclamation and stale-worker fencing tests."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.auth.model import UserModel
from adapter.knowledge.model import KnowledgeDocumentModel
from adapter.knowledge.repository import KnowledgeRepository
from app.knowledge.worker import KnowledgeWorker
from domain.knowledge.model import KnowledgeBase

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
BACKEND_DIR = Path(__file__).resolve().parents[2]
DB_CONNECT_ARGS = {"timeout": 8, "command_timeout": 12}
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="test PostgreSQL URL is not configured"),
]


def _upgrade_schema(database_url: str, schema: str) -> None:
    child_env = os.environ.copy()
    child_env["ATHENA_TEST_DATABASE_URL"] = database_url
    child_env["KNOWLEDGE_TEST_ALEMBIC_INI"] = str(BACKEND_DIR / "alembic.ini")
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
    assert result.returncode == 0, "knowledge Alembic migration subprocess failed"


def test_expired_job_reclaim_fences_late_worker_publish() -> None:
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("test PostgreSQL URL is not configured")

    async def scenario() -> None:
        schema = f"athena_knowledge_jobs_{token_hex(8)}"
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
            factory = async_sessionmaker(engine, expire_on_commit=False)
            repository = KnowledgeRepository(factory)
            now = datetime(2026, 1, 1, tzinfo=UTC)
            async with factory() as session, session.begin():
                session.add(
                    UserModel(
                        id="lease_owner",
                        email="lease-owner@example.com",
                        password_hash="test",
                        display_name=None,
                        status="active",
                        created_at=now,
                        updated_at=now,
                    )
                )
            await repository.create_base(
                KnowledgeBase("lease_kb", "lease_owner", "lease", None, now, now)
            )
            await repository.create_text_document(
                owner_id="lease_owner",
                base_id="lease_kb",
                title="profile mismatch",
                text="must not be embedded by an incompatible worker",
                request_key="profile-create",
                request_hash="profile-hash",
                content_hash="profile-content-hash",
                document_id="profile_doc",
                version_id="profile_ver",
                job_id="profile_job",
                profile_id="profile-v1",
                at=now,
            )
            mismatch_worker = KnowledgeWorker(
                repository,
                cast(Any, object()),
                cast(Any, object()),
                cast(Any, object()),
                cast(Any, object()),
                cast(Any, object()),
                isolate_parsing=False,
                profile_id="profile-v2",
            )
            assert await mismatch_worker.run_once()
            async with factory() as session:
                profile_state = await session.scalar(
                    text("SELECT state FROM knowledge_jobs WHERE id='profile_job'")
                )
                profile_version_state = await session.scalar(
                    text("SELECT state FROM knowledge_document_versions WHERE id='profile_ver'")
                )
                profile_chunks = await session.scalar(
                    text("SELECT count(*) FROM knowledge_chunks WHERE version_id='profile_ver'")
                )
            assert profile_state == "failed"
            assert profile_version_state == "failed"
            assert profile_chunks == 0

            await repository.create_text_document(
                owner_id="lease_owner",
                base_id="lease_kb",
                title="doc",
                text="lease fencing content",
                request_key="lease-create",
                request_hash="lease-hash",
                content_hash="lease-content-hash",
                document_id="lease_doc",
                version_id="lease_ver",
                job_id="lease_job",
                profile_id="profile-v1",
                at=now,
            )
            first = await repository.claim_job(lease_seconds=120)
            assert first is not None and first.fencing_token == 1
            assert first.profile_id == "profile-v1"
            assert not await repository.publish_job(
                replace(first, profile_id="different-profile"),
                chunks=[
                    (
                        "incompatible profile must not publish",
                        {"kind": "text"},
                        5,
                        "incompatible profile must not publish",
                        [0.0] * 1024,
                    )
                ],
                warnings=[],
            )
            assert await repository.heartbeat_job(
                first.job_id, first.fencing_token, lease_seconds=120
            )

            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE knowledge_jobs SET lease_until = now() - interval '1 second' "
                        "WHERE id = 'lease_job'"
                    )
                )
            second = await repository.claim_job(lease_seconds=120)
            assert second is not None and second.fencing_token == 2
            assert not await repository.heartbeat_job(
                first.job_id, first.fencing_token, lease_seconds=120
            )
            assert not await repository.publish_job(
                first,
                chunks=[("stale contents", {"kind": "text"}, 2, "stale contents", [0.0] * 1024)],
                warnings=[],
            )
            assert await repository.publish_job(
                second,
                chunks=[
                    ("current contents", {"kind": "text"}, 2, "current contents", [0.1] * 1024)
                ],
                warnings=[],
            )
            async with factory() as session:
                state = await session.scalar(
                    text("SELECT state FROM knowledge_jobs WHERE id='lease_job'")
                )
                count = await session.scalar(
                    text("SELECT count(*) FROM knowledge_chunks WHERE version_id='lease_ver'")
                )
            assert state == "done"
            assert count == 1

            async def verify_lease_expiry_after_lock_wait(operation: str) -> None:
                document_id = f"expiry_{operation}_doc"
                version_id = f"expiry_{operation}_version"
                job_id = f"expiry_{operation}_job"
                await repository.create_text_document(
                    owner_id="lease_owner",
                    base_id="lease_kb",
                    title=f"lease expiry {operation}",
                    text=f"lease must expire while {operation} waits",
                    request_key=f"expiry-{operation}-create",
                    request_hash=f"expiry-{operation}-hash",
                    content_hash=f"expiry-{operation}-content-hash",
                    document_id=document_id,
                    version_id=version_id,
                    job_id=job_id,
                    profile_id="profile-v1",
                    at=now + timedelta(seconds=1),
                )
                expiring = await repository.claim_job(lease_seconds=1)
                assert expiring is not None and expiring.job_id == job_id
                started = asyncio.Event()

                async def contend() -> bool | None:
                    started.set()
                    if operation == "heartbeat":
                        return await repository.heartbeat_job(
                            expiring.job_id,
                            expiring.fencing_token,
                            lease_seconds=30,
                        )
                    if operation == "publish":
                        return await repository.publish_job(
                            expiring,
                            chunks=[
                                (
                                    "late publication",
                                    {"kind": "text"},
                                    2,
                                    "late publication",
                                    [0.4] * 1024,
                                )
                            ],
                            warnings=[],
                        )
                    await repository.fail_job(expiring, error_code="late_failure", retryable=False)
                    return None

                async with factory() as lock_session, lock_session.begin():
                    await lock_session.execute(
                        text("SELECT id FROM knowledge_jobs WHERE id = :job_id FOR UPDATE"),
                        {"job_id": job_id},
                    )
                    await lock_session.execute(
                        text(
                            "UPDATE knowledge_jobs SET lease_until = "
                            "clock_timestamp() + interval '1 second' WHERE id = :job_id"
                        ),
                        {"job_id": job_id},
                    )
                    contender_task = asyncio.create_task(contend())
                    await started.wait()
                    await lock_session.execute(text("SELECT pg_sleep(1.2)"))
                result = await contender_task
                if operation != "fail":
                    assert result is False
                async with factory() as session:
                    job_state = await session.scalar(
                        text("SELECT state FROM knowledge_jobs WHERE id = :job_id"),
                        {"job_id": job_id},
                    )
                    version_state = await session.scalar(
                        text(
                            "SELECT state FROM knowledge_document_versions WHERE id = :version_id"
                        ),
                        {"version_id": version_id},
                    )
                    chunks = await session.scalar(
                        text(
                            "SELECT count(*) FROM knowledge_chunks WHERE version_id = :version_id"
                        ),
                        {"version_id": version_id},
                    )
                assert job_state == "processing"
                assert version_state == "processing"
                assert chunks == 0
                async with factory() as session, session.begin():
                    await session.execute(
                        text(
                            "UPDATE knowledge_jobs SET next_attempt_at = "
                            "clock_timestamp() + interval '1 day' WHERE id = :job_id"
                        ),
                        {"job_id": job_id},
                    )

            await verify_lease_expiry_after_lock_wait("heartbeat")
            await verify_lease_expiry_after_lock_wait("publish")
            await verify_lease_expiry_after_lock_wait("fail")

            _, lock_race_version = await repository.replace_text_document(
                owner_id="lease_owner",
                base_id="lease_kb",
                document_id="lease_doc",
                text="publish failure concurrent lock order",
                request_key="lock-race-replace",
                request_hash="lock-race-hash",
                content_hash="lock-race-content-hash",
                version_id="lock_race_version",
                job_id="lock_race_job",
                profile_id="profile-v1",
                at=now + timedelta(seconds=1),
            )
            lock_race_job = await repository.claim_job(lease_seconds=120)
            assert lock_race_job is not None
            assert lock_race_job.version_id == lock_race_version
            concurrent_results = await asyncio.gather(
                repository.publish_job(
                    lock_race_job,
                    chunks=[
                        (
                            "publish failure concurrent lock order",
                            {"kind": "text"},
                            4,
                            "publish failure concurrent lock order",
                            [0.15] * 1024,
                        )
                    ],
                    warnings=[],
                ),
                repository.fail_job(
                    lock_race_job, error_code="concurrent_fixture_failure", retryable=False
                ),
                return_exceptions=True,
            )
            assert not any(isinstance(result, BaseException) for result in concurrent_results)
            async with factory() as session:
                race_task_state = await session.scalar(
                    text("SELECT state FROM knowledge_jobs WHERE id = 'lock_race_job'")
                )
                race_version_state = await session.scalar(
                    text(
                        "SELECT state FROM knowledge_document_versions "
                        "WHERE id = 'lock_race_version'"
                    )
                )
            assert (race_task_state, race_version_state) in {
                ("done", "ready"),
                ("failed", "failed"),
            }

            _, pending_version = await repository.replace_text_document(
                owner_id="lease_owner",
                base_id="lease_kb",
                document_id="lease_doc",
                text="late publish after base delete",
                request_key="late-replace",
                request_hash="late-replace-hash",
                content_hash="late-replace-content-hash",
                version_id="late_version",
                job_id="late_job",
                profile_id="profile-v1",
                at=now + timedelta(seconds=1),
            )
            late_job = await repository.claim_job(lease_seconds=120)
            assert late_job is not None and late_job.version_id == pending_version
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE knowledge_jobs SET lease_until = now() - interval '1 second' "
                        "WHERE id = :job_id"
                    ),
                    {"job_id": late_job.job_id},
                )
            await repository.fail_job(late_job, error_code="late_failure", retryable=False)
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE knowledge_jobs SET next_attempt_at = now() + interval '1 day' "
                        "WHERE id = :job_id"
                    ),
                    {"job_id": late_job.job_id},
                )
            async with factory() as session:
                late_state = await session.scalar(
                    text("SELECT state FROM knowledge_jobs WHERE id = 'late_job'")
                )
                late_version_state = await session.scalar(
                    text("SELECT state FROM knowledge_document_versions WHERE id = 'late_version'")
                )
            assert late_state == "processing"
            assert late_version_state == "processing"

            await repository.create_base(
                KnowledgeBase("sibling_kb", "lease_owner", "sibling", None, now, now)
            )
            async with factory() as session, session.begin():
                session.add(
                    KnowledgeDocumentModel(
                        id="sibling_doc",
                        owner_id="lease_owner",
                        knowledge_base_id="sibling_kb",
                        title="sibling",
                        active_version_id=None,
                        pending_version_id=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
            assert await repository.soft_delete_base("lease_owner", "lease_kb", now)
            assert not await repository.publish_job(
                late_job,
                chunks=[("late publish", {"kind": "text"}, 2, "late publish", [0.2] * 1024)],
                warnings=[],
            )
            assert (
                await repository.delete_document("lease_owner", "sibling_kb", "sibling_doc") == []
            )
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE knowledge_jobs SET next_attempt_at = now() + interval '1 day' "
                        "WHERE kind = 'cleanup' AND target_id = 'doc:sibling_doc'"
                    )
                )
            cleanup = await repository.claim_job(lease_seconds=120)
            assert cleanup is not None
            assert cleanup.kind == "cleanup"
            assert cleanup.target_id == "lease_kb"
            await repository.fail_job(cleanup, error_code="storage_delete_failed", retryable=True)
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE knowledge_jobs SET next_attempt_at = now() - interval '1 second' "
                        "WHERE id = :job_id"
                    ),
                    {"job_id": cleanup.job_id},
                )
            cleanup_retry = await repository.claim_job(lease_seconds=120)
            assert cleanup_retry is not None and cleanup_retry.target_id == "lease_kb"
            assert cleanup_retry.fencing_token == cleanup.fencing_token + 1
            assert not await repository.finish_cleanup_job(cleanup)
            assert await repository.finish_cleanup_job(cleanup_retry)
            async with factory() as session:
                sibling_cleanup_state = await session.scalar(
                    text(
                        "SELECT state FROM knowledge_jobs "
                        "WHERE kind = 'cleanup' AND target_id = 'doc:sibling_doc'"
                    )
                )
            assert sibling_cleanup_state == "queued"
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE knowledge_jobs SET next_attempt_at = now() - interval '1 second' "
                        "WHERE kind = 'cleanup' AND target_id = 'doc:sibling_doc'"
                    )
                )
            sibling_cleanup = await repository.claim_job(lease_seconds=120)
            assert sibling_cleanup is not None
            assert sibling_cleanup.target_id == "doc:sibling_doc"
            assert await repository.finish_cleanup_job(sibling_cleanup)
            async with factory() as session:
                sibling_after_cleanup = await session.get(KnowledgeDocumentModel, "sibling_doc")
                assert sibling_after_cleanup is not None
                assert sibling_after_cleanup.deleted_time is not None
                remaining_sibling_versions = await session.scalar(
                    text(
                        "SELECT count(*) FROM knowledge_document_versions "
                        "WHERE document_id = 'sibling_doc'"
                    )
                )
                sibling_cleanup_state = await session.scalar(
                    text(
                        "SELECT state FROM knowledge_jobs "
                        "WHERE kind = 'cleanup' AND target_id = 'doc:sibling_doc'"
                    )
                )
                assert remaining_sibling_versions == 0
                assert sibling_cleanup_state == "done"
            assert await repository.get_base("lease_owner", "lease_kb") is None
            with pytest.raises(LookupError, match="knowledge_base_not_found"):
                await repository.validate_search_scope("lease_owner", ["lease_kb"])
        finally:
            await engine.dispose()
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin.dispose()

    asyncio.run(scenario())
