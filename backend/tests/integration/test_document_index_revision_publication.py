"""Document index revisions are published only while their lease is current."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from domain.files import FileChunk, FileLocator, FileMetadata
from domain.leases import LeaseKey, LeaseLostError
from infrastructure.integrations.neo4j.adapter import Neo4jGraphIndex
from infrastructure.integrations.pgvector.indexers import PostgresFileVectorIndex
from infrastructure.persistence.postgres.models import (
    AttachmentModel,
    Base,
    ExecutionLeaseModel,
    FileChunkModel,
    KnowledgeBaseModel,
    KnowledgeDocumentJobModel,
)
from infrastructure.persistence.postgres.repositories.document_job_repository import (
    PostgresDocumentJobRepository,
)
from infrastructure.persistence.postgres.repositories.file_repository import (
    PostgresAttachmentRepository,
    PostgresFileChunkRepository,
)
from infrastructure.persistence.postgres.repositories.lease_center import (
    PostgresLeaseCenter,
)


@pytest_asyncio.fixture
async def document_revision_store():
    """Create the small document schema in an isolated PostgreSQL namespace."""
    database_url = os.getenv("ATHENA_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"document_revision_{uuid4().hex}"
    attachment_id = f"attachment-{uuid4().hex}"
    session_id = f"session-{uuid4().hex}"
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(url)
    engine = None
    schema_created = False
    try:
        try:
            async with admin_engine.begin() as connection:
                await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception as exc:
            pytest.skip(f"pgvector is unavailable in the test database: {exc}")

        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True
        scoped_url = url.update_query_dict(
            {"options": f"-c search_path={schema},public"}
        )
        engine = create_async_engine(scoped_url)
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        KnowledgeBaseModel.__table__,
                        AttachmentModel.__table__,
                        FileChunkModel.__table__,
                        KnowledgeDocumentJobModel.__table__,
                        ExecutionLeaseModel.__table__,
                    ],
                    checkfirst=False,
                )
            )

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        leases = PostgresLeaseCenter(sessions)
        jobs = PostgresDocumentJobRepository(sessions, lease_center=leases)
        attachments = PostgresAttachmentRepository(sessions, lease_center=leases)
        chunks = PostgresFileChunkRepository(sessions, lease_center=leases)
        now = datetime.now(timezone.utc).isoformat()
        async with sessions() as session, session.begin():
            session.add(
                AttachmentModel(
                    id=attachment_id,
                    logical_document_id=attachment_id,
                    document_version=1,
                    session_id=session_id,
                    knowledge_base_id=None,
                    message_id=None,
                    filename="revision.txt",
                    mime_type="text/plain",
                    size_bytes=32,
                    sha256=uuid4().hex,
                    storage_key=f"documents/{attachment_id}",
                    adapter_name="text",
                    adapter_version="1",
                    status="uploaded",
                    capabilities_json="[]",
                    parsed_metadata_json="{}",
                    error_message=None,
                    active_index_revision_id=None,
                    created_at=now,
                    updated_at=now,
                    deleted_time=None,
                )
            )
            session.add(
                KnowledgeDocumentJobModel(
                    job_id=f"job-{attachment_id}",
                    attachment_id=attachment_id,
                    status="queued",
                    attempt=0,
                    available_at=now,
                    result_json=None,
                    error_json=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        yield SimpleNamespace(
            sessions=sessions,
            jobs=jobs,
            attachments=attachments,
            chunks=chunks,
            attachment_id=attachment_id,
            session_id=session_id,
        )
    finally:
        if engine is not None:
            await engine.dispose()
        if schema_created:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


class _FixedEmbedder:
    """Small deterministic embedder for exercising the real pgvector column."""

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [_vector() for _ in texts]

    async def embed_query(self, _text: str) -> list[float]:
        return _vector()


def _vector() -> list[float]:
    return [1.0, *([0.0] * 1023)]


def _chunk(attachment_id: str, revision_id: str, suffix: str) -> FileChunk:
    return FileChunk(
        id=f"chunk-{suffix}-{uuid4().hex}",
        attachment_id=attachment_id,
        index_revision_id=revision_id,
        ordinal=0,
        content=f"lease revision marker {suffix}",
        token_count=4,
        locator=FileLocator(),
        metadata=FileMetadata(),
    )


async def _seed_first_revision(store, *, graph=None):
    claimed = await store.jobs.claim_next()
    assert claimed is not None
    lease = claimed["lease"]
    attachment = await store.attachments.get(store.attachment_id)
    assert attachment is not None
    old_revision = str(uuid4())
    old_chunk = _chunk(store.attachment_id, old_revision, "old")
    await store.chunks.replace_for_attachment(
        store.attachment_id, [old_chunk], lease=lease
    )
    vector_index = PostgresFileVectorIndex(store.sessions, _FixedEmbedder())
    await vector_index.index_attachment(attachment, [old_chunk], lease=lease)
    if graph is not None:
        await graph.index_attachment(attachment, [old_chunk], lease=lease)
    async with store.sessions() as session, session.begin():
        attachment_row = await session.get(
            AttachmentModel, store.attachment_id, with_for_update=True
        )
        assert attachment_row is not None
        attachment_row.active_index_revision_id = old_revision
        attachment_row.status = "ready"
    return claimed, attachment, old_revision, old_chunk, vector_index


async def _expire(store, grant) -> None:
    async with store.sessions() as session, session.begin():
        await session.execute(
            update(ExecutionLeaseModel)
            .where(
                ExecutionLeaseModel.resource_type == grant.key.resource_type,
                ExecutionLeaseModel.resource_id == grant.key.resource_id,
                ExecutionLeaseModel.fencing_token == grant.fencing_token,
            )
            .values(lease_until=func.clock_timestamp() - text("INTERVAL '1 second'"))
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_document_claim_skips_more_than_32_active_leases(document_revision_store) -> None:
    """前 32 条活跃任务不会遮住后面的待领取文档。"""
    store = document_revision_store
    now = datetime.now(timezone.utc)
    earlier = (now - timedelta(days=1)).isoformat()
    async with store.sessions() as session, session.begin():
        for index in range(32):
            attachment_id = f"active-attachment-{index:02}"
            session.add(AttachmentModel(
                id=attachment_id, logical_document_id=attachment_id,
                document_version=1, session_id=store.session_id,
                filename="active.txt", mime_type="text/plain", size_bytes=1,
                sha256=uuid4().hex, storage_key=attachment_id, status="processing",
                created_at=earlier, updated_at=earlier,
            ))
        await session.flush()
        for index in range(32):
            attachment_id = f"active-attachment-{index:02}"
            session.add(KnowledgeDocumentJobModel(
                job_id=f"active-job-{index:02}", attachment_id=attachment_id,
                status="running", attempt=1, available_at=earlier,
                created_at=earlier, updated_at=earlier,
            ))
            session.add(ExecutionLeaseModel(
                resource_type="document_ingestion", resource_id=attachment_id,
                owner_id="other", fencing_token=1, state="active",
                lease_until=now + timedelta(minutes=5), created_at=now, updated_at=now,
            ))
    claimed = await store.jobs.claim_next()
    assert claimed is not None and claimed["attachment_id"] == store.attachment_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_document_writes_reject_another_attachments_lease(document_revision_store) -> None:
    """有效 grant 只能用于其所属附件的分块和向量。"""
    store = document_revision_store
    lease = await PostgresLeaseCenter(store.sessions).acquire(
        LeaseKey("document_ingestion", "other-attachment"),
        owner_id="other", lease_seconds=60,
    )
    assert lease is not None
    attachment = await store.attachments.get(store.attachment_id)
    assert attachment is not None
    chunks = [_chunk(store.attachment_id, str(uuid4()), "wrong-lease")]
    with pytest.raises(ValueError, match="document_ingestion_lease_mismatch"):
        await store.chunks.replace_for_attachment(store.attachment_id, chunks, lease=lease)
    with pytest.raises(ValueError, match="document_ingestion_lease_mismatch"):
        await PostgresFileVectorIndex(store.sessions, _FixedEmbedder()).index_attachment(
            attachment, chunks, lease=lease
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_document_revision_publish_filters_stale_chunks_and_vectors(
    document_revision_store,
) -> None:
    """A late old grant cannot write or expose its revision after takeover."""
    store = document_revision_store
    first, attachment, old_revision, old_chunk, vector_index = await _seed_first_revision(
        store
    )
    old_grant = first["lease"]
    await _expire(store, old_grant)

    current = await store.jobs.claim_next()
    assert current is not None
    new_grant = current["lease"]
    assert new_grant.fencing_token == old_grant.fencing_token + 1
    new_revision = str(uuid4())
    new_chunk = _chunk(store.attachment_id, new_revision, "current")
    await store.chunks.replace_for_attachment(
        store.attachment_id, [new_chunk], lease=new_grant
    )
    await vector_index.index_attachment(attachment, [new_chunk], lease=new_grant)
    await store.jobs.mark_succeeded(
        current["job_id"],
        {"chunks": 1, "index_revision_id": new_revision},
        lease=new_grant,
    )
    async with store.sessions() as session:
        published_revision = await session.scalar(
            select(AttachmentModel.active_index_revision_id).where(
                AttachmentModel.id == store.attachment_id
            )
        )
    assert published_revision == new_revision

    with pytest.raises(LeaseLostError):
        await store.chunks.replace_for_attachment(
            store.attachment_id,
            [_chunk(store.attachment_id, old_revision, "late")],
            lease=old_grant,
        )
    assert [chunk.id for chunk in await store.chunks.list_by_attachment(store.attachment_id)] == [
        new_chunk.id
    ]
    vector_hits = await vector_index.query("marker", attachment_ids={store.attachment_id})
    assert [hit["chunk_id"] for hit in vector_hits] == [new_chunk.id]
    graph_candidates = await store.chunks.filter_active_index_revisions(
        [
            {
                "attachment_id": store.attachment_id,
                "index_revision_id": old_revision,
            },
            {
                "attachment_id": store.attachment_id,
                "index_revision_id": new_revision,
            },
        ]
    )
    assert [candidate["index_revision_id"] for candidate in graph_candidates] == [
        new_revision
    ]

    assert await store.attachments.soft_delete(
        store.attachment_id, session_id=store.session_id
    )
    assert await vector_index.query("marker", attachment_ids={store.attachment_id}) == []
    assert await store.chunks.filter_active_index_revisions(
        [
            {
                "attachment_id": store.attachment_id,
                "index_revision_id": new_revision,
            }
        ]
    ) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_late_neo4j_revision_is_filtered_after_publish_and_delete(
    document_revision_store,
) -> None:
    """Real Neo4j candidates are checked against PostgreSQL publication state."""
    uri = os.getenv("ATHENA_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("ATHENA_TEST_NEO4J_URI is not configured")
    neo4j = pytest.importorskip("neo4j")
    username = os.getenv("ATHENA_TEST_NEO4J_USERNAME", "neo4j")
    password = os.getenv("ATHENA_TEST_NEO4J_PASSWORD", "")
    database = os.getenv("ATHENA_TEST_NEO4J_DATABASE", "neo4j")
    driver = neo4j.AsyncGraphDatabase.driver(uri, auth=(username, password))
    graph = Neo4jGraphIndex(driver, database=database)
    store = document_revision_store
    try:
        await graph.delete_attachment(store.attachment_id)
        first, attachment, old_revision, _, _ = await _seed_first_revision(
            store, graph=graph
        )
        old_grant = first["lease"]
        await _expire(store, old_grant)
        current = await store.jobs.claim_next()
        assert current is not None
        new_revision = str(uuid4())
        new_chunk = _chunk(store.attachment_id, new_revision, "current")
        await store.chunks.replace_for_attachment(
            store.attachment_id, [new_chunk], lease=current["lease"]
        )
        await graph.index_attachment(attachment, [new_chunk], lease=current["lease"])
        await store.jobs.mark_succeeded(
            current["job_id"],
            {"chunks": 1, "index_revision_id": new_revision},
            lease=current["lease"],
        )

        # Simulate an old process finishing its graph write after publication.
        late_chunk = _chunk(store.attachment_id, old_revision, "late")
        await graph.index_attachment(attachment, [late_chunk], lease=old_grant)
        filtered = []
        cursor = None
        while not filtered:
            candidates = await graph.query_context(
                "lease revision marker", limit=1, after_id=cursor
            )
            assert candidates
            filtered = await store.chunks.filter_active_index_revisions(candidates)
            cursor = str(candidates[-1]["id"])
        assert len(filtered) == 1
        assert filtered[0]["index_revision_id"] == new_revision

        assert await store.attachments.soft_delete(
            store.attachment_id, session_id=store.session_id
        )
        candidates_after_delete = await graph.query_context(
            "lease revision marker", limit=1
        )
        assert await store.chunks.filter_active_index_revisions(candidates_after_delete) == []
    finally:
        await graph.delete_attachment(store.attachment_id)
        await driver.close()
