"""Isolated PostgreSQL drill for the destructive knowledge reset script."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex

import pytest
import reset_knowledge_documents as reset_script
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.auth.model import UserModel
from adapter.knowledge.model import (
    KnowledgeBaseModel,
    KnowledgeChunkModel,
    KnowledgeDocumentModel,
    KnowledgeJobModel,
    KnowledgeRequestModel,
    KnowledgeSourceModel,
    KnowledgeUploadReservationModel,
    KnowledgeUploadSlotModel,
    KnowledgeVersionModel,
    ProjectKnowledgeBaseModel,
)
from adapter.knowledge.repository import KnowledgeRepository
from adapter.project.model import ProjectModel

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
DB_CONNECT_ARGS = {"timeout": 8, "command_timeout": 20}
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="test PostgreSQL URL is not configured",
    ),
]


async def _seed_widget_data(session_factory: async_sessionmaker[AsyncSession]) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    owner_hash = "a" * 64
    source_hash = "b" * 64
    source_key = f"{owner_hash}/ksrc_seed/{source_hash[:2]}/{source_hash}"
    reservation_hash = "c" * 64
    reservation_key = f"{owner_hash}/ksrc_reservation/{reservation_hash[:2]}/{reservation_hash}"
    async with session_factory() as session, session.begin():
        session.add(
            UserModel(
                id="reset_owner",
                email="reset-owner@example.com",
                password_hash="test",
                display_name=None,
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ProjectModel(
                id="reset_project",
                owner_id="reset_owner",
                name="preserved project",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            KnowledgeBaseModel(
                id="reset_kb",
                owner_id="reset_owner",
                name="preserved knowledge base",
                description=None,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            ProjectKnowledgeBaseModel(
                project_id="reset_project",
                knowledge_base_id="reset_kb",
                owner_id="reset_owner",
                created_at=now,
            )
        )
        session.add(
            KnowledgeSourceModel(
                id="reset_source",
                owner_id="reset_owner",
                storage_key=source_key,
                filename="reset.txt",
                media_type="text/plain",
                size_bytes=7,
                sha256=source_hash,
                created_at=now,
            )
        )
        session.add(
            KnowledgeDocumentModel(
                id="reset_doc",
                owner_id="reset_owner",
                knowledge_base_id="reset_kb",
                title="reset document",
                active_version_id="reset_version",
                pending_version_id=None,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            KnowledgeVersionModel(
                id="reset_version",
                document_id="reset_doc",
                owner_id="reset_owner",
                source_id="reset_source",
                source_text=None,
                content_hash=source_hash,
                profile_id="profile-v1",
                processing_config={},
                state="ready",
                error_code=None,
                warnings=[],
                chunk_count=1,
                created_at=now,
                completed_at=now,
            )
        )
        await session.flush()
        session.add(
            KnowledgeChunkModel(
                id="reset_chunk",
                document_id="reset_doc",
                version_id="reset_version",
                ordinal=0,
                content="fixture",
                locator={"kind": "text"},
                token_count=1,
                search_document=func.to_tsvector("simple", "fixture"),
                embedding=[0.1] * 1024,
            )
        )
        session.add(
            KnowledgeJobModel(
                id="reset_job",
                owner_id="reset_owner",
                kind="ingestion",
                target_id="reset_version",
                state="done",
                attempt=1,
                lease_until=None,
                fencing_token=1,
                next_attempt_at=now,
                error_code=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            KnowledgeRequestModel(
                owner_id="reset_owner",
                operation="create",
                request_key="reset-request",
                request_hash="request-hash",
                document_id="reset_doc",
                version_id="reset_version",
                created_at=now,
            )
        )
        session.add(
            KnowledgeUploadReservationModel(
                owner_id="reset_owner",
                operation="create",
                request_key="reset-reservation",
                reservation_token="reset-token",
                request_hash="reservation-hash",
                base_id="reset_kb",
                document_id="reset_document",
                version_id="reset_pending_version",
                source_id="reset_pending_source",
                job_id="reset_pending_job",
                storage_key=reservation_key,
                size_bytes=3,
                state="reserved",
                fencing_token=0,
                lease_until=None,
                created_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
        slot = await session.scalar(
            select(KnowledgeUploadSlotModel).order_by(KnowledgeUploadSlotModel.slot_id).limit(1)
        )
        assert slot is not None
        slot.token = "stale-slot-token"
        slot.lease_until = now + timedelta(hours=1)


def test_reset_preview_execute_rollback_repeat_and_slot_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("test PostgreSQL URL is not configured")

    async def scenario() -> None:
        schema = f"athena_knowledge_reset_{token_hex(8)}"
        admin = create_async_engine(database_url, poolclass=NullPool, connect_args=DB_CONNECT_ARGS)
        engine = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args={
                **DB_CONNECT_ARGS,
                "server_settings": {"search_path": f"{schema},public"},
            },
        )
        try:
            async with admin.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            migration_config = Config(str(ALEMBIC_INI))
            migration_config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
            migration_config.attributes["connect_args"] = {
                "server_settings": {"search_path": f"{schema},public"}
            }
            migration_config.attributes["version_table_schema"] = schema
            await asyncio.to_thread(command.upgrade, migration_config, "head")
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            await _seed_widget_data(session_factory)

            preview = await reset_script.run_reset(
                database_url,
                execute=False,
                schema=schema,
            )
            assert preview.before["knowledge_documents"] == 1
            assert preview.before["knowledge_bases"] == 1
            assert preview.before["project_knowledge_bases"] == 1
            assert preview.slots_in_use_before == 1
            assert preview.after["knowledge_documents"] == 1
            assert preview.executed is False
            if (password := make_url_password(database_url)) is not None:
                assert password not in preview.database

            original_counts = reset_script._counts
            calls = 0

            async def fail_after_delete(
                connection: object, schema: str | None = None
            ) -> dict[str, int]:
                del schema
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("simulated rollback")
                return await original_counts(connection)  # type: ignore[arg-type]

            monkeypatch.setattr(reset_script, "_counts", fail_after_delete)
            with pytest.raises(RuntimeError, match="simulated rollback"):
                await reset_script.run_reset(database_url, execute=True, schema=schema)
            monkeypatch.setattr(reset_script, "_counts", original_counts)

            async with session_factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM knowledge_documents")) == 1
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM knowledge_upload_slots WHERE token IS NOT NULL")
                    )
                    == 1
                )

            report = await reset_script.run_reset(
                database_url,
                execute=True,
                schema=schema,
            )
            assert report.executed is True
            assert report.after["knowledge_documents"] == 0
            assert report.after["knowledge_chunks"] == 0
            assert report.after["knowledge_sources"] == 0
            assert report.after["knowledge_bases"] == 1
            assert report.after["project_knowledge_bases"] == 1
            async with session_factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM projects")) == 1
                assert (
                    await session.scalar(text("SELECT count(*) FROM knowledge_upload_slots")) == 4
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM knowledge_upload_slots "
                            "WHERE token IS NOT NULL OR lease_until IS NOT NULL"
                        )
                    )
                    == 0
                )

            repeated = await reset_script.run_reset(
                database_url,
                execute=True,
                schema=schema,
            )
            assert all(repeated.before[table] == 0 for table in reset_script._DOCUMENT_TABLES)

            repository = KnowledgeRepository(session_factory)
            assert await repository.acquire_upload_slot("reused-slot", lease_seconds=601)
            await repository.release_upload_slot("reused-slot")
        finally:
            await engine.dispose()
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin.dispose()

    asyncio.run(scenario())


def test_reset_preview_is_non_exclusive_while_execute_obeys_lock_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("test PostgreSQL URL is not configured")

    async def scenario() -> None:
        schema = f"athena_knowledge_reset_lock_{token_hex(8)}"
        admin = create_async_engine(database_url, poolclass=NullPool, connect_args=DB_CONNECT_ARGS)
        engine = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args={
                **DB_CONNECT_ARGS,
                "server_settings": {"search_path": f"{schema},public"},
            },
        )
        blocker = create_async_engine(
            database_url,
            poolclass=NullPool,
            connect_args={
                **DB_CONNECT_ARGS,
                "server_settings": {"search_path": f"{schema},public"},
            },
        )
        blocker_connection = None
        blocker_transaction = None
        try:
            async with admin.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            migration_config = Config(str(ALEMBIC_INI))
            migration_config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
            migration_config.attributes["connect_args"] = {
                "server_settings": {"search_path": f"{schema},public"}
            }
            migration_config.attributes["version_table_schema"] = schema
            await asyncio.to_thread(command.upgrade, migration_config, "head")
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            await _seed_widget_data(session_factory)

            blocker_connection = await blocker.connect()
            blocker_transaction = await blocker_connection.begin()
            await blocker_connection.execute(
                text("LOCK TABLE knowledge_documents IN ACCESS SHARE MODE")
            )
            preview = await asyncio.wait_for(
                reset_script.run_reset(database_url, execute=False, schema=schema),
                timeout=2,
            )
            assert preview.before["knowledge_documents"] == 1
            assert preview.after["knowledge_documents"] == 1

            monkeypatch.setattr(reset_script, "_LOCK_TIMEOUT", "100ms")
            with pytest.raises(DBAPIError, match="lock timeout"):
                await asyncio.wait_for(
                    reset_script.run_reset(database_url, execute=True, schema=schema),
                    timeout=2,
                )
            await blocker_transaction.rollback()
            blocker_transaction = None
            await blocker_connection.close()
            blocker_connection = None

            report = await asyncio.wait_for(
                reset_script.run_reset(database_url, execute=True, schema=schema),
                timeout=5,
            )
            assert report.executed is True
            assert report.after["knowledge_documents"] == 0
        finally:
            if blocker_transaction is not None:
                await blocker_transaction.rollback()
            if blocker_connection is not None:
                await blocker_connection.close()
            await blocker.dispose()
            await engine.dispose()
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin.dispose()

    asyncio.run(scenario())


def make_url_password(database_url: str) -> str | None:
    from sqlalchemy.engine import make_url

    return make_url(database_url).password
