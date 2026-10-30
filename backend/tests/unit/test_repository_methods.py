"""注入式 PostgreSQL repository 的行为覆盖。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from domain.files import (
    AdapterInfo, Attachment, AttachmentStatus, FileArtifact, FileChunk,
    FileLocator, FileMetadata,
)
from domain.knowledge import KnowledgeBase
from domain.sessions import Message, MessageRole, Session
from domain.tools import (
    JsonSchema, MCPServer, MCPServerConfig, RiskLevel, ToolConfig, ToolExecutionMode,
)
from tests.fakes.leases import LeaseCenter, lease_grant
from infrastructure.persistence.postgres.repositories.file_artifact_repository import (
    PostgresAdapterRegistryRepository, PostgresFileArtifactRepository,
)
from infrastructure.persistence.postgres.repositories.file_repository import (
    PostgresAttachmentRepository, PostgresFileChunkRepository,
)
from infrastructure.persistence.postgres.repositories.knowledge_base_repository import PostgresKnowledgeBaseRepository
from infrastructure.persistence.postgres.repositories.knowledge_document_repository import PostgresKnowledgeDocumentRepository
from infrastructure.persistence.postgres.repositories.message_repository import PostgresMessageRepository
from infrastructure.persistence.postgres.repositories.mcp_repository import PostgresMCPServerRepository
from infrastructure.persistence.postgres.repositories.session_repository import PostgresSessionRepository
from infrastructure.persistence.postgres.repositories.tool_repository import PostgresToolConfigRepository


class Result:
    rowcount = 1

    def __init__(self, *, scalar=None, rows=()):
        self.scalar = scalar
        self.rows = list(rows)

    def scalar_one_or_none(self):
        return self.scalar

    def one_or_none(self):
        return self.rows[0] if self.rows else None

    def scalars(self):
        return self

    def all(self):
        return self.rows


class DB:
    def __init__(self):
        self.results = []
        self.get_values = []
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def begin(self):
        return self

    def add(self, value):
        self.added.append(value)

    def add_all(self, values):
        self.added.extend(values)

    async def execute(self, statement):
        return self.results.pop(0) if self.results else Result()

    async def scalar(self, statement):
        return self.results.pop(0).scalar if self.results else None

    async def get(self, model, key):
        return self.get_values.pop(0) if self.get_values else None


def factory(db):
    def make():
        return db
    return make


def now():
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def session():
    stamp = now()
    return Session("s-1", "Title", None, stamp, stamp)


def message():
    return Message("m-1", "s-1", MessageRole.USER, "hello", timestamp=now())


def attachment(*, knowledge=False, version=1):
    stamp = now()
    return Attachment(
        id="a-1", logical_document_id="doc-1", document_version=version,
        session_id=None if knowledge else "s-1", knowledge_base_id="kb-1" if knowledge else None,
        message_id=None, filename="a.txt", mime_type="text/plain", size_bytes=3,
        sha256="abc", storage_key="abc/a", adapter_name=None, adapter_version=None,
        status=AttachmentStatus.UPLOADED, capabilities=(), metadata=FileMetadata(),
        error_message=None, created_at=stamp, updated_at=stamp,
    )


def attachment_row(value=None):
    value = value or attachment()
    return SimpleNamespace(
        id=value.id, logical_document_id=value.logical_document_id,
        document_version=value.document_version, session_id=value.session_id,
        knowledge_base_id=value.knowledge_base_id, message_id=value.message_id,
        filename=value.filename, mime_type=value.mime_type, size_bytes=value.size_bytes,
        sha256=value.sha256, storage_key=value.storage_key, adapter_name=value.adapter_name,
        adapter_version=value.adapter_version, status=value.status.value,
        capabilities_json=[], parsed_metadata_json={}, error_message=None,
        created_at=value.created_at, updated_at=value.updated_at,
        deleted_time=None,
    )


@pytest.mark.asyncio
async def test_session_and_message_repositories_cover_crud_and_attachment_reader():
    db = DB(); repo = PostgresSessionRepository(factory(db))
    value = session()
    assert await repo.create(value) == value
    session_row = SimpleNamespace(
        id="s-1", title="Title", active_run_id=None, last_run_sequence=0,
        created_at=now(), updated_at=now(),
        project_id="project-1",
        last_activity_at=now(), compression_summary=None,
        last_compressed_message_id=None, last_summarized_message_id=None,
    )
    db.results = [Result(scalar=session_row)]
    assert (await repo.get("s-1")).id == "s-1"
    db.results = [Result(rows=[session_row])]
    assert len(await repo.list_sessions()) == 1
    assert await repo.save(value) == value
    db.results = [Result(scalar=session_row), Result(scalar="s-1")]
    assert await repo.delete("s-1")

    messages = DB()
    async def read(ids):
        return {item: [] for item in ids}
    mrepo = PostgresMessageRepository(factory(messages), attachment_reader=read)
    messages.results = [Result(scalar=session_row)]
    assert await mrepo.save(message()) == "m-1"
    row = SimpleNamespace(id="m-1", session_id="s-1", role="user", content="hello",
        tool_calls_json=[], tool_call_id=None, run_id=None, tool_call_record_id=None,
        tool_name=None, message_type=None, timestamp=now(), deleted_time=None)
    messages.results = [Result(scalar=row)]
    assert (await mrepo.get("m-1")).content == "hello"
    messages.results = [Result(rows=[row])]
    assert len(await mrepo.list_by_session("s-1", limit=2)) == 1
    messages.results = [Result(rows=[row])]
    assert len(await mrepo.list_after("s-1", "m-0")) == 1


@pytest.mark.asyncio
async def test_file_and_knowledge_repositories_cover_versions_and_cleanup():
    db = DB(); ar = PostgresAttachmentRepository(factory(db)); value = attachment()
    assert await ar.create(value) == value
    db.results = [Result(scalar=attachment_row(value))]
    assert (await ar.get("a-1", session_id="s-1")).id == "a-1"
    db.results = [Result(rows=[attachment_row(value)])]
    assert len(await ar.list_by_session("s-1")) == 1
    db.results = [Result(rows=[attachment_row(value)])]
    assert len(await ar.list_by_knowledge_base("kb-1")) == 1
    assert await ar.save(value) == value
    db.results = [
        Result(scalar=None),
        Result(scalar=attachment_row(value)),
        Result(scalar="a-1"),
        Result(scalar=None),
    ]
    assert await ar.soft_delete("a-1", session_id="s-1")

    chunks = PostgresFileChunkRepository(factory(db), lease_center=LeaseCenter())
    chunk = FileChunk("c-1", "a-1", 0, "hello", 1, FileLocator(), FileMetadata())
    db.results = [Result(scalar=attachment_row(value))]
    await chunks.replace_for_attachment(
        "a-1", [chunk], lease=lease_grant("document_ingestion", "a-1")
    )
    db.results = [Result(rows=[SimpleNamespace(id="c-1", attachment_id="a-1", ordinal=0,
        content="hello", token_count=1, locator_json={}, metadata_json={}, native_score=None)])]
    assert (await chunks.list_by_attachment("a-1"))[0].id == "c-1"

    docs = PostgresKnowledgeDocumentRepository(
        factory(db), lease_center=LeaseCenter()
    ); doc = attachment(knowledge=True)
    assert await docs.create(doc) == doc
    db.results = [Result(scalar=attachment_row(doc))]
    assert (await docs.get("a-1", knowledge_base_id="kb-1")).id == "a-1"
    db.results = [Result(rows=[attachment_row(doc)])]
    assert len(await docs.list_documents("kb-1")) == 1
    db.results = [Result(scalar=attachment_row(doc)), Result(rows=[attachment_row(doc)])]
    assert len(await docs.list_versions("a-1", knowledge_base_id="kb-1")) == 1
    db.results = [Result(scalar=attachment_row(doc))]
    assert await docs.latest_for_filename("a.txt", knowledge_base_id="kb-1")
    db.results = [
        Result(scalar=None),
        Result(scalar=attachment_row(doc)),
        Result(scalar="a-1"),
        Result(scalar=None),
        Result(),
        Result(),
    ]
    assert await docs.soft_delete("a-1", knowledge_base_id="kb-1")


@pytest.mark.asyncio
async def test_tool_mcp_knowledge_and_artifact_repositories_cover_crud():
    db = DB(); stamp = now()
    config = ToolConfig("search", ToolExecutionMode.NATIVE, None, None, "Search", JsonSchema(), RiskLevel.LOW, False, True, stamp, stamp)
    tools = PostgresToolConfigRepository(factory(db)); db.get_values = [None]
    assert await tools.upsert(config) == config
    row = SimpleNamespace(tool_name="search", execution_mode="native", server_name=None, remote_name=None,
        description="Search", parameters_schema_json={}, risk_level="low", require_approval=False,
        enabled=True, created_at=stamp, updated_at=stamp)
    db.get_values = [row]; assert (await tools.get("search")).tool_name == "search"
    db.results = [Result(rows=[row])]; assert len(await tools.list_tools()) == 1
    db.get_values = [row]; assert await tools.update(config) == config

    server = MCPServer("srv", MCPServerConfig("python", (), {}, None, "none", True), stamp)
    mcp = PostgresMCPServerRepository(factory(db)); db.get_values = [None]; assert await mcp.upsert(server) == server
    mrow = SimpleNamespace(name="srv", config_json={'command': 'python', 'args': [], 'env': {}, 'enabled': True}, created_at=stamp, deleted_time=None)
    db.results = [Result(scalar=mrow)]; assert (await mcp.get("srv")).name == "srv"
    db.results = [Result(rows=[mrow])]; assert len(await mcp.list_servers()) == 1
    db.results = [Result()]; assert await mcp.delete("srv")

    kb = KnowledgeBase("kb-1", "KB", "", 0, 0, 0, stamp, stamp)
    kbr = PostgresKnowledgeBaseRepository(factory(db)); assert await kbr.create(kb) == kb
    krow = SimpleNamespace(id="kb-1", name="KB", description="", created_at=stamp, updated_at=stamp)
    db.results = [Result(scalar=krow), Result(rows=[attachment_row(attachment(knowledge=True))])]
    assert (await kbr.get("kb-1")).document_count == 1
    db.results = [Result(rows=[krow]), Result(rows=[attachment_row(attachment(knowledge=True))])]
    assert len(await kbr.list_knowledge_bases()) == 1
    assert await kbr.save(kb) == kb
    db.results = [Result(scalar="kb-1")]; assert await kbr.delete("kb-1")

    artifact = FileArtifact("f-1", "a-1", "text", "cache", "body", None, FileMetadata(), stamp)
    far = PostgresFileArtifactRepository(factory(db)); db.results = [Result(scalar=None)]
    assert await far.get("cache") is None
    assert await far.put(artifact) == artifact
    registry = PostgresAdapterRegistryRepository(factory(db))
    await registry.replace_all([AdapterInfo("text", "1", ("text/plain",), (".txt",), ("text",))])
    db.results = [Result(rows=[SimpleNamespace(name="text", version="1", mime_types_json=["text/plain"], extensions_json=[".txt"], capabilities_json=["text"], enabled=True)])]
    assert (await registry.list_adapters())[0].name == "text"
