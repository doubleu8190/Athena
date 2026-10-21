"""Behavior coverage for the target production dependency graph."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bootstrap import build_default_dependencies, create_app
from configuration.settings import Settings
from domain.approval import ApprovalDecision, ApprovalRequest
from domain.events import ApplicationEvent, EventDurability, EventType
from domain.memory import MemoryListRequest, MemorySearchRequest, MemoryWriteCommand
from domain.runs import CommandStatus, CommandType, RunCommand, RunStatus
from infrastructure.integrations.file_parsers.native import NativeFileAdapterRegistry, NativeFileParser, fixed_size_chunker
from infrastructure.integrations.indexing import DisabledGraphIndexer, DisabledVectorIndexer
from infrastructure.integrations.storage.local import LocalFileStorage
from infrastructure.persistence.postgres.repositories.document_job_repository import PostgresDocumentJobRepository
from infrastructure.persistence.postgres.repositories import (
    PostgresApprovalRepository,
    PostgresEventRepository,
    PostgresMemoryRepository,
    PostgresRetrievalRepository,
    PostgresRunRepository,
    PostgresOrchestrationOutboxRepository,
)
from infrastructure.persistence.postgres.engine import PostgresEngine
from infrastructure.persistence.postgres.options import PostgresOptions
from infrastructure.persistence.postgres.models import (
    ApprovalRecordModel,
    OrchestrationOutboxModel,
)
from workers import WorkerSupervisor
from workers import KnowledgeDocumentWorker
from bootstrap.check import check_app

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Result:
    rowcount = 1

    def __init__(self, *, scalar=None, rows=()):
        self.scalar = scalar
        self.rows = list(rows)

    def scalar_one_or_none(self):
        return self.scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows.pop(0) if self.rows else self.scalar


class DB:
    def __init__(self):
        self.results = []
        self.get_values = []
        self.scalar_values = []
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return self

    def add(self, value):
        self.added.append(value)

    async def execute(self, _statement):
        return self.results.pop(0) if self.results else Result()

    async def get(self, _model, _key, **_kwargs):
        return self.get_values.pop(0) if self.get_values else None

    async def scalar(self, _statement):
        return self.scalar_values.pop(0) if self.scalar_values else None


def factory(db):
    @asynccontextmanager
    async def provide():
        yield db

    return provide


def command(command_id="c-1", run_id="r-1"):
    return RunCommand(command_id, CommandType.RUN_START, "s-1", run_id, {"message": "hello"}, NOW)


@pytest.mark.asyncio
async def test_target_runtime_repositories_cover_queue_events_approval_memory_and_retrieval():
    db = DB()
    runs = PostgresRunRepository(factory(db))
    db.get_values = [None, None]
    result = await runs.enqueue_command(command())
    assert result.command_id == "c-1" and db.added
    db.get_values = [SimpleNamespace(command_id="c-1", session_id="s-1", run_id="r-1", command_type="run.start", status="queued", payload_json='{"message":"hello"}', result_json=None, error_json=None)]
    assert (await runs.get_command("c-1")).command_id == "c-1"
    db.results = [Result(scalar=SimpleNamespace(command_id="c-1", command_type="run.start", session_id="s-1", run_id="r-1", payload_json='{"message":"hello"}', queued_at=NOW.isoformat(), schema_version=1, status="queued"))]
    claimed = await runs.claim_next_command()
    assert claimed is not None and claimed.command_id == "c-1"
    await runs.complete_command("c-1", status=CommandStatus.COMPLETED, result={"ok": True})
    run_row = SimpleNamespace(run_id="r-1", session_id="s-1", status="created", root_thread_id="t", error_json=None, created_at=NOW.isoformat(), updated_at=NOW.isoformat())
    db.results = [Result(rows=[run_row])]
    assert (await runs.list_for_session("s-1"))[0].run_id == "r-1"
    db.get_values = [run_row]
    assert (await runs.get("r-1")).status == "created"
    db.results = [Result(scalar=run_row)]
    assert (await runs.get_active_for_session("s-1")).run_id == "r-1"
    await runs.update_status("r-1", RunStatus.RUNNING)
    await runs.complete("r-1", {"answer": "ok"})
    await runs.fail("r-1", {"message": "bad"})
    await runs.cancel("r-1")

    events = PostgresEventRepository(factory(db))
    db.scalar_values = [0]
    stored = await events.publish(ApplicationEvent(EventType.RUN_STARTED, EventDurability.DURABLE, "s-1"))
    assert stored.session_seq == 1
    queue, watermark = await events.open_subscription("s-1")
    assert watermark == 0
    db.results = [Result(rows=[SimpleNamespace(event_type="run.started", durability="durable", session_id="s-1", session_seq=1, run_id=None, message_id=None, attachment_id=None, stream_id=None, stream_type=None, is_complete=0, parent_run_id=None, transition_id=None, payload_json="{}", occurred_at=NOW.isoformat())])]
    assert len(await events.list_events_between("s-1")) == 1
    await events.publish_realtime(ApplicationEvent(EventType.MESSAGE_DELTA, EventDurability.REALTIME, "s-1"))
    await events.close_subscription("s-1", queue)

    approvals = PostgresApprovalRepository(factory(db))
    request = ApprovalRequest("a-1", "s-1", "r-1", "call", "shell", {"x": 1}, "high", created_at=NOW)
    db.get_values = [None]
    assert await approvals.create(request)
    row = SimpleNamespace(approval_id="a-1", session_id="s-1", run_id="r-1", approval_batch_id="b-1", plan_id=None, task_id="t-1", tool_call_id="call", tool_name="shell", arguments_json='{"x":1}', risk_level="high", status="pending", decision=None, created_at=NOW.isoformat(), decided_at=None)
    db.get_values = [row]
    assert (await approvals.get("a-1")).approval_id == "a-1"
    db.results = [Result(rows=[row])]
    assert await approvals.list_pending("s-1")
    row_resolved = SimpleNamespace(**{**row.__dict__, "status": "resolved", "decision": "approved", "decided_at": NOW.isoformat()})
    db.results = [Result(rows=[row_resolved])]
    assert await approvals.list_history("s-1")
    await approvals.resolve("a-1", run_id="r-1", task_id="t-1", decision=ApprovalDecision.APPROVED)
    db.get_values = [row]
    await approvals.resolve_batch("b-1", {"a-1": ApprovalDecision.DENIED}, run_id="r-1")

    memory = PostgresMemoryRepository(factory(db))
    mem_row = SimpleNamespace(id="m-1", content="hello memory", metadata_json='{"x":1}', source_kind="chat", status="active", created_at=NOW.isoformat(), expires_at=None, last_accessed_at=None, access_count=0, last_observed_at=NOW.isoformat(), valid_until=None, validity_status="valid")
    db.results = [Result(rows=[mem_row])]
    assert (await memory.search(MemorySearchRequest("hello")))[0].id == "m-1"
    db.results = [Result(rows=[mem_row])]
    db.scalar_values = [1]
    assert (await memory.list_memories(MemoryListRequest())).total == 1
    db.get_values = [mem_row]
    assert (await memory.get("m-1")).content == "hello memory"
    db.get_values = [mem_row]
    assert await memory.revisions("m-1")
    await memory.create_memory(MemoryWriteCommand("created", operation_key="m-2"))
    await memory.revise("m-1", "revised", metadata={"x": 2})
    await memory.update_validity("m-1", "valid")
    await memory.delete("m-1")
    assert await memory.flush_access_stats() == 0

    retrieval = PostgresRetrievalRepository(factory(db))
    retrieval_row = SimpleNamespace(run_id="rr", query="q", scope="session", status="done", config_json="{}", candidate_count=1, selected_count=1, injected_count=0, created_at=NOW.isoformat(), completed_at=None)
    db.results = [Result(rows=[retrieval_row])]
    db.scalar_values = [1]
    assert (await retrieval.list_runs(scope="session"))[0][0]["run_id"] == "rr"
    db.get_values = [retrieval_row]
    assert (await retrieval.get_run("rr"))["query"] == "q"


@pytest.mark.asyncio
async def test_postgres_event_repository_reuses_transition_identity():
    db = DB()
    events = PostgresEventRepository(factory(db))
    existing = SimpleNamespace(
        event_type="run.cancelled",
        durability="durable",
        session_id="s-1",
        session_seq=7,
        run_id="r-1",
        message_id=None,
        attachment_id=None,
        stream_id=None,
        stream_type=None,
        is_complete=0,
        parent_run_id=None,
        transition_id="orchestration-outbox:message-1",
        payload_json='{"status":"cancelled"}',
        occurred_at=NOW.isoformat(),
    )
    db.results = [Result(scalar=existing)]

    value = await events.publish(
        ApplicationEvent(
            "run.cancelled",
            EventDurability.DURABLE,
            "s-1",
            run_id="r-1",
            transition_id="orchestration-outbox:message-1",
            occurred_at=NOW,
            payload={"status": "cancelled"},
        )
    )

    assert value.session_seq == 7
    assert db.added == []

    db.results = [Result(scalar=existing)]
    with pytest.raises(ValueError, match="transition_id_reused_with_different_event"):
        await events.publish(
            ApplicationEvent(
                "run.cancelled",
                EventDurability.DURABLE,
                "s-1",
                run_id="r-1",
                transition_id="orchestration-outbox:message-1",
                occurred_at=NOW,
                payload={"status": "completed"},
            )
        )


@pytest.mark.asyncio
async def test_postgres_orchestration_outbox_claims_and_acknowledges_lease():
    db = DB()
    outbox = SimpleNamespace(
        message_id="message-1",
        event_type="RunCancelled",
        plan_id="plan-1",
        plan_version=1,
        round_id="round-1",
        occurred_at=NOW,
        payload_json={"cancellation_epoch": 3},
        status="pending",
        attempt=0,
        lease_owner=None,
        lease_until=None,
    )
    db.results = [Result(rows=[(outbox, "s-1", "r-1")])]
    repository = PostgresOrchestrationOutboxRepository(
        factory(db), clock=lambda: NOW
    )

    lease = await repository.claim_next_event(
        "dispatcher-1", lease_seconds=30
    )

    assert lease is not None
    assert lease.run_id == "r-1"
    assert lease.session_id == "s-1"
    assert lease.attempt == 1
    assert outbox.status == "processing"
    assert outbox.lease_owner == "dispatcher-1"
    assert await repository.mark_event_published(lease)


@pytest.mark.asyncio
async def test_target_file_parser_and_document_job_queue():
    root = Path("/tmp/athena-target-parser")
    storage = LocalFileStorage(root, 1024)

    async def chunks():
        yield b"hello\nworld"

    blob = await storage.save_stream(chunks())
    attachment = SimpleNamespace(storage_key=blob.storage_key, adapter_name="text", adapter_version="1", capabilities=("text",))
    parsed = await NativeFileParser(storage).parse(attachment)
    assert parsed.text == "hello\nworld"
    registry = NativeFileAdapterRegistry()
    assert ".txt" in registry.supported_extensions()
    assert registry.select("a.txt", "text/plain").name == "text"
    assert registry.select("a.pdf", "application/pdf").name == "pdf"
    await NativeFileParser(storage).parse(SimpleNamespace(filename="data.csv", storage_key=blob.storage_key, adapter_name="csv", adapter_version="1", capabilities=("text",)))
    values = fixed_size_chunker(max_characters=5)("hello world")
    assert [value.ordinal for value in values] == [0, 1, 2]
    await DisabledVectorIndexer().index_attachment(attachment, values)
    await DisabledGraphIndexer().delete_attachment("a")
    with pytest.raises(ValueError):
        fixed_size_chunker(max_characters=0)
    await DisabledVectorIndexer().delete_attachment("a")
    await DisabledGraphIndexer().index_attachment(attachment, values)
    await storage.cleanup_unreferenced({blob.storage_key})
    orphan = root / "orphan"
    orphan.write_bytes(b"x")
    assert await storage.cleanup_unreferenced({blob.storage_key}) == 1
    async def too_large():
        yield b"0123456789"
    with pytest.raises(ValueError):
        await LocalFileStorage(root, 2).save_stream(too_large())

    async def save_body(body):
        async def one():
            yield body
        return await storage.save_stream(one())
    for filename, body in [("data.pdf", b"not-pdf"), ("data.docx", b"not-docx"), ("data.xlsx", b"not-xlsx")]:
        invalid = await save_body(body)
        with pytest.raises(Exception):
            await NativeFileParser(storage).parse(SimpleNamespace(filename=filename, storage_key=invalid.storage_key, adapter_name="x", adapter_version="1", capabilities=()))
    image = await save_body(b"image")
    assert (await NativeFileParser(storage).parse(SimpleNamespace(filename="data.png", storage_key=image.storage_key, adapter_name="x", adapter_version="1", capabilities=()))).metadata.values["format"] == "png"
    await storage.discard(blob.storage_key)
    with pytest.raises(FileNotFoundError):
        await storage.read(blob.storage_key)

    import sys
    from types import ModuleType
    class Page:
        def extract_text(self): return "pdf text"
    class Reader:
        def __init__(self, value): self.pages = [Page()]
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=Reader))
    monkeypatch.setitem(sys.modules, "docx", SimpleNamespace(Document=lambda value: SimpleNamespace(paragraphs=[SimpleNamespace(text="docx text")])) )
    class Sheet:
        title = "Sheet1"
        def iter_rows(self, values_only=True): return [("a", "b"), (1, 2)]
    monkeypatch.setitem(sys.modules, "openpyxl", SimpleNamespace(load_workbook=lambda *args, **kwargs: SimpleNamespace(worksheets=[Sheet()])))
    for filename, expected in [("a.pdf", "pdf"), ("a.docx", "docx"), ("a.xlsx", "xlsx"), ("a.csv", "csv"), ("a.json", "json")]:
        value = await save_body(b'{"x": 1}' if expected in {"csv", "json"} else b"data")
        parsed = await NativeFileParser(storage).parse(SimpleNamespace(filename=filename, storage_key=value.storage_key, adapter_name="x", adapter_version="1", capabilities=()))
        assert parsed.text
    monkeypatch.undo()


    db = DB()
    jobs = PostgresDocumentJobRepository(factory(db))
    db.results = [Result(scalar=None)]
    assert await jobs.enqueue("a-1")
    db.results = [Result(scalar=SimpleNamespace(job_id="j", attachment_id="a-1", status="queued", attempt=0, available_at="2000-01-01"))]
    claimed = await jobs.claim_next()
    assert claimed["attachment_id"] == "a-1"
    await jobs.mark_succeeded("j", {"chunks": 1})
    await jobs.mark_failed("j", "bad", retry=False)
    await jobs.cancel("a-1")
    db.results = [Result(scalar=SimpleNamespace(job_id="j", attachment_id="a-1", status="failed", attempt=1, available_at="2000-01-01"))]
    assert await jobs.enqueue("a-1")
    db.results = [Result(scalar=None)]
    assert await jobs.claim_next() is None


def test_postgres_dependency_graph_is_explicit(monkeypatch):
    monkeypatch.setenv("ATHENA_DATABASE_BACKEND", "postgres")
    graph = build_default_dependencies()
    assert len(graph.resources) == 5
    app = create_app(dependencies=graph)
    assert "/api/sessions" in {route.path for route in app.routes}
    assert graph.provider("memory_service_factory") is not None
    assert graph.provider("orchestration_repository_factory") is not None
    assert graph.provider("orchestration_service_factory") is not None
    monkeypatch.setenv("ATHENA_WORKERS_ENABLED", "true")
    worker_graph = build_default_dependencies()
    assert len(worker_graph.resources) == 6
    monkeypatch.delenv("ATHENA_WORKERS_ENABLED")
    monkeypatch.delenv("ATHENA_DATABASE_BACKEND")
    assert Settings().database_backend == "postgres"


@pytest.mark.asyncio
async def test_worker_supervisor_manages_start_run_stop_workers():
    class Worker:
        def __init__(self):
            self.started = False
            self.stopped = False
            self.finished = asyncio.Event()

        async def run(self):
            self.started = True
            await self.finished.wait()

        def stop(self):
            self.stopped = True
            self.finished.set()

    worker = Worker()
    supervisor = WorkerSupervisor([worker])
    await supervisor.start()
    await asyncio.sleep(0)
    assert worker.started
    await supervisor.stop()
    assert worker.stopped


@pytest.mark.asyncio
async def test_worker_supervisor_supports_start_protocol_and_rejects_invalid_worker():
    class Started:
        def __init__(self): self.value = []
        async def start(self): self.value.append("start")
        async def stop(self): self.value.append("stop")
    started = Started()
    supervisor = WorkerSupervisor([started])
    await supervisor.start()
    await supervisor.stop()
    assert started.value == ["start", "stop"]
    with pytest.raises(TypeError):
        await WorkerSupervisor([object()]).start()


@pytest.mark.asyncio
async def test_document_worker_run_and_stop_when_queue_is_empty():
    class Queue:
        async def claim_next(self, **kwargs): return None
        async def mark_succeeded(self, *args): pass
        async def mark_failed(self, *args, **kwargs): pass
    class Ingest:
        async def process(self, attachment_id): return {}
    worker = KnowledgeDocumentWorker(Queue(), Ingest())
    task = asyncio.create_task(worker.run(poll_interval=0.001))
    await asyncio.sleep(0.005)
    worker.stop()
    await task


def test_startup_probe_checks_default_graph():
    check_app()


@pytest.mark.asyncio
async def test_postgres_resource_lifecycle_without_connecting_to_database(monkeypatch):
    class Engine:
        async def dispose(self):
            self.disposed = True

    engine = Engine()
    monkeypatch.setattr(
        "infrastructure.persistence.postgres.engine.create_async_engine",
        lambda *args, **kwargs: engine,
    )
    resource = PostgresEngine(PostgresOptions(Settings().postgres_url, create_schema=False))
    with pytest.raises(RuntimeError):
        async with resource.session():
            pass
    await resource.start()
    await resource.start()
    await resource.stop()
    await resource.stop()
    assert getattr(engine, "disposed", False)


@pytest.mark.asyncio
async def test_postgres_resource_schema_and_session_context(monkeypatch):
    class Connection:
        async def execute(self, _statement):
            raise RuntimeError("vector extension unavailable")
        async def rollback(self):
            self.rolled_back = True
        async def run_sync(self, callback):
            callback(object())

    class Begin:
        async def __aenter__(self):
            return Connection()
        async def __aexit__(self, *_args):
            return False

    class Engine:
        def begin(self):
            return Begin()
        async def dispose(self):
            return None

    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False

    engine = Engine()
    monkeypatch.setattr(
        "infrastructure.persistence.postgres.engine.create_async_engine",
        lambda *args, **kwargs: engine,
    )
    monkeypatch.setattr(
        "infrastructure.persistence.postgres.engine.async_sessionmaker",
        lambda *args, **kwargs: lambda: Session(),
    )
    monkeypatch.setattr("infrastructure.persistence.postgres.engine.Base.metadata.create_all", lambda _connection: None)
    resource = PostgresEngine(PostgresOptions(Settings().postgres_url, create_schema=True))
    await resource.start()
    async with resource.session() as value:
        assert isinstance(value, Session)
    await resource.stop()

@pytest.mark.asyncio
async def test_memory_access_is_atomic_and_management_reads_do_not_write():
    """验证访问原子累加、ID 去重以及管理查询无写入，无业务异常。"""
    from sqlalchemy.dialects import postgresql

    class CapturingDB(DB):
        async def execute(self, statement):
            self.statements.append(statement)
            return await super().execute(statement)

    db = CapturingDB()
    db.statements = []
    repository = PostgresMemoryRepository(factory(db))
    assert await repository.record_access([]) == 0
    assert not db.statements
    assert await repository.record_access(["m-1", "m-1", "m-2"]) == 1
    compiled = db.statements[0].compile(dialect=postgresql.dialect())
    assert "coalesce(memories.access_count" in str(compiled)
    assert "last_accessed_at=" in str(compiled)
    assert any(isinstance(value, list) and set(value) == {"m-1", "m-2"} for value in compiled.params.values())
    db.statements.clear()
    await repository.search(MemorySearchRequest("fact"))
    await repository.list_memories(MemoryListRequest(expired_only=True))
    assert all(statement.is_select for statement in db.statements)
    search_sql = str(db.statements[0].compile(dialect=postgresql.dialect()))
    assert "memories.expires_at" in search_sql and "memories.valid_until" in search_sql
    list_sql = str(db.statements[1].compile(dialect=postgresql.dialect()))
    assert "<= now()" in list_sql


@pytest.mark.asyncio
async def test_postgres_task_approval_creation_writes_requested_outbox():
    db = DB()
    repository = PostgresApprovalRepository(factory(db))
    db.get_values = [
        None,
        SimpleNamespace(
            plan_id="p-1",
            run_id="r-1",
            status="running",
            current_version=3,
            active_round_id="round-3",
        ),
    ]
    request = ApprovalRequest(
        approval_id="approval-1",
        session_id="s-1",
        run_id="r-1",
        tool_call_id="call-1",
        tool_name="write_file",
        arguments={"path": "report.txt"},
        risk_level="high",
        task_id="task-1",
        plan_id="p-1",
        execution_id="execution-1",
        worker_thread_id="task:task-1:exec:1",
        interrupt_id="interrupt-1",
        args_hash="args-hash",
        operation_key="operation-1",
        created_at=NOW,
    )

    assert await repository.create(request)
    approval_row = next(
        item for item in db.added if isinstance(item, ApprovalRecordModel)
    )
    outbox = next(
        item for item in db.added if isinstance(item, OrchestrationOutboxModel)
    )
    assert approval_row.approval_id == "approval-1"
    assert outbox.event_type == "ApprovalRequested"
    assert outbox.dedupe_key == "approval:approval-1:requested"
    assert outbox.plan_version == 3
    assert outbox.round_id == "round-3"
