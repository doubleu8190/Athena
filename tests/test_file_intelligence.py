from __future__ import annotations
import asyncio
from datetime import datetime
import sqlite3
import sys
import types
from unittest.mock import AsyncMock

import pytest

from athena.config.settings import Settings
from athena.runtime.graph_runtime import LangGraphRuntime
from athena.core.files.adapters import (
    ExcelAdapter,
    ImageAdapter,
    PdfAdapter,
    TextAdapter,
    WordAdapter,
)
from athena.core.files.base import ExtractedUnit, ExtractionContext
from athena.core.files.registry import AdapterRegistry
from athena.core.files.runtime import FileAccessError, FileIntelligenceRuntime
from athena.core.files.storage import FileTooLargeError, StorageLayer
from athena.core.tools.catalog import ToolCatalogService, ToolRegistry
from athena.core.tools.providers.files import build_file_tool_specs
from athena.core.llm.tokens import conservative_text_token_count
from athena.infrastructure.sqlite.database import Database
from athena.models import Message, MessageRole
from athena.models.file import FileChunk
from tests.fakes import make_tool_manager


class _FakeLLM:
    async def ainvoke(self, _messages):
        class Response:
            content = "summary"

        return Response()

    def count_text_tokens(self, text: str) -> int:
        return conservative_text_token_count(text)


def _make_runtime(repository, settings: Settings) -> FileIntelligenceRuntime:
    return FileIntelligenceRuntime(
        repository,
        _FakeLLM(),
        _FakeLLM(),
        settings=settings,
            event_publisher=AsyncMock(),
    )


async def _chunks(*values: bytes):
    for value in values:
        yield value


def _context(path, workspace, filename: str | None = None, mime_type: str = "") -> ExtractionContext:
    return ExtractionContext(path=path, workspace=workspace, filename=filename or path.name, mime_type=mime_type)


@pytest.mark.asyncio
async def test_content_addressed_storage_deduplicates_and_limits(tmp_path):
    storage = StorageLayer(tmp_path, max_upload_bytes=8)
    first = await storage.save_stream(_chunks(b"same"))
    second = await storage.save_stream(_chunks(b"sa", b"me"))
    assert first.storage_key == second.storage_key
    assert len(storage.blob_keys()) == 1

    with pytest.raises(FileTooLargeError):
        await storage.save_stream(_chunks(b"123456789"))


@pytest.mark.asyncio
async def test_parse_search_message_binding_and_session_isolation(tmp_path):
    db = Database(str(tmp_path / "files.db"))
    await db.connect()
    try:
        await db.sessions.create("one")
        await db.sessions.create("two")
        settings = Settings(
            _env_file=None,
            sqlite_db_path=str(tmp_path / "files.db"),
            chromadb_path=str(tmp_path / "chroma"),
            file_storage_path=str(tmp_path / "storage"),
        )
        runtime = _make_runtime(db.files, settings)
        blob = await runtime.storage.save_stream(_chunks(b"alpha beta\nsecond line\n"))
        attachment = await db.files.create_attachment(
            session_id="one", filename="notes.txt", mime_type="text/plain",
            size_bytes=blob.size_bytes, sha256=blob.sha256, storage_key=blob.storage_key,
        )
        parsed = await runtime.parse_attachment(attachment.id)
        await runtime.index_attachment(attachment.id)
        result = await runtime.search_file("one", attachment.id, "alpha")
        assert parsed["chunk_count"] == 1
        assert result["results"][0]["locator"]["path"] == "notes.txt"

        with pytest.raises(FileAccessError):
            await runtime.read_file("two", attachment.id)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_knowledge_base_documents_are_reusable_only_by_bound_sessions(tmp_path):
    """独立知识库文档应能跨会话复用，同时保持显式授权边界。"""
    db = Database(str(tmp_path / "knowledge-base.db"))
    await db.connect()
    try:
        await db.sessions.create("one")
        await db.sessions.create("two")
        knowledge_base = await db.knowledge_bases.create("产品文档")
        document = await db.files.create_attachment(
            knowledge_base_id=knowledge_base.id,
            filename="guide.txt",
            mime_type="text/plain",
            size_bytes=5,
            sha256="a" * 64,
            storage_key="blobs/aa/placeholder",
        )

        assert await db.files.get_accessible_attachment("one", document.id) is None

        await db.knowledge_bases.bind_session("one", knowledge_base.id)
        await db.knowledge_bases.bind_session("two", knowledge_base.id)

        assert await db.files.get_accessible_attachment("one", document.id) is not None
        assert await db.files.get_accessible_attachment("two", document.id) is not None
        assert [item.id for item in await db.files.list_accessible_attachments("one")] == [document.id]

        await db.knowledge_bases.unbind_session("one", knowledge_base.id)
        assert await db.files.get_accessible_attachment("one", document.id) is None
        assert await db.files.get_accessible_attachment("two", document.id) is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_existing_attachment_table_migrates_to_independent_ownership(tmp_path):
    """旧数据库启动时应解除 session_id 非空限制并保留原附件。"""
    database_path = tmp_path / "legacy-attachments.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE attachments (
            id VARCHAR PRIMARY KEY,
            session_id VARCHAR NOT NULL,
            message_id VARCHAR,
            filename VARCHAR NOT NULL,
            mime_type VARCHAR NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 VARCHAR NOT NULL,
            storage_key VARCHAR NOT NULL,
            adapter_name VARCHAR,
            adapter_version VARCHAR,
            status VARCHAR NOT NULL DEFAULT 'uploaded',
            capabilities_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            error_message TEXT,
            created_at VARCHAR NOT NULL,
            updated_at VARCHAR NOT NULL,
            deleted_time VARCHAR
        )
        """
    )
    now = datetime.now().isoformat()
    connection.execute(
        """
        INSERT INTO attachments (
            id, session_id, filename, mime_type, size_bytes, sha256,
            storage_key, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("legacy", "session", "old.txt", "text/plain", 3, "b" * 64, "blob", now, now),
    )
    connection.commit()
    connection.close()

    db = Database(str(database_path))
    await db.connect()
    try:
        legacy = await db.files.get_attachment("legacy")
        assert legacy is not None
        assert legacy.session_id == "session"
        assert legacy.knowledge_base_id is None

        knowledge_base = await db.knowledge_bases.create("迁移后知识库")
        document = await db.files.create_attachment(
            knowledge_base_id=knowledge_base.id,
            filename="new.txt",
            mime_type="text/plain",
            size_bytes=3,
            sha256="c" * 64,
            storage_key="new-blob",
        )
        assert document.session_id is None
        assert document.knowledge_base_id == knowledge_base.id
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_process_attachment_index_failure_marks_attachment_failed(tmp_path):
    db = Database(str(tmp_path / "index-failure.db"))
    await db.connect()
    try:
        await db.sessions.create("session")
        settings = Settings(
            _env_file=None,
            sqlite_db_path=str(tmp_path / "index-failure.db"),
            chromadb_path=str(tmp_path / "chroma"),
            file_storage_path=str(tmp_path / "storage"),
        )
        runtime = _make_runtime(db.files, settings)
        attachment = await db.files.create_attachment(
            session_id="session", filename="notes.txt", mime_type="text/plain",
            size_bytes=1, sha256="5" * 64, storage_key="blobs/55/placeholder",
        )
        await db.files.replace_chunks(
            attachment.id,
            [FileChunk(id="chunk", attachment_id=attachment.id, ordinal=0, content="text")],
        )

        class BrokenCollection:
            def delete(self, **_kwargs):
                raise RuntimeError("vector index unavailable")

        runtime._collection = BrokenCollection()

        runtime.parse_attachment = AsyncMock(return_value={"chunk_count": 1})
        graph_runtime = LangGraphRuntime.__new__(LangGraphRuntime)
        graph_runtime._db = db
        graph_runtime._events = AsyncMock()
        graph_runtime._file_runtime = runtime
        graph_runtime._file_parse_semaphore = asyncio.Semaphore(1)
        graph_runtime._file_embedding_semaphore = asyncio.Semaphore(1)

        result = await graph_runtime.process_attachment(
            attachment.id, "message-1", "session", "run-1"
        )

        current = await db.files.get_attachment(attachment.id)
        assert current is not None
        assert result["status"] == "failed"
        assert current.status.value == "failed"
        assert current.error_message == "vector index unavailable"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_attachment_cannot_be_bound_to_different_messages(tmp_path):
    db = Database(str(tmp_path / "attachment-binding.db"))
    await db.connect()
    try:
        await db.sessions.create("session")
        for message_id in ("message-1", "message-2"):
            await db.messages.save(
                Message(
                    id=message_id,
                    session_id="session",
                    role=MessageRole.USER,
                    content=message_id,
                    timestamp=datetime.now(),
                )
            )
        attachment = await db.files.create_attachment(
            session_id="session", filename="notes.txt", mime_type="text/plain",
            size_bytes=1, sha256="6" * 64, storage_key="blobs/66/placeholder",
        )

        await db.files.bind_message("session", "message-1", [attachment.id])

        with pytest.raises(ValueError, match="附件已关联其他消息"):
            await db.files.bind_message("session", "message-2", [attachment.id])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_parse_csv_preserves_suffix_for_table_artifact(tmp_path):
    db = Database(str(tmp_path / "csv-artifacts.db"))
    await db.connect()
    try:
        await db.sessions.create("session")
        settings = Settings(
            _env_file=None,
            sqlite_db_path=str(tmp_path / "csv-artifacts.db"),
            chromadb_path=str(tmp_path / "chroma"),
            file_storage_path=str(tmp_path / "storage"),
        )
        runtime = _make_runtime(db.files, settings)
        blob = await runtime.storage.save_stream(_chunks(b"name,value\nalpha,1\nbeta,2\n"))
        attachment = await db.files.create_attachment(
            session_id="session", filename="data.csv", mime_type="text/csv",
            size_bytes=blob.size_bytes, sha256=blob.sha256, storage_key=blob.storage_key,
        )

        parsed = await runtime.parse_attachment(attachment.id)
        key = FileIntelligenceRuntime.cache_key(
            attachment, "tables", {}, "2.0", settings.primary_llm.model
        )
        artifact = await db.files.get_artifact(key)

        assert parsed["row_count"] == 2
        assert parsed["columns"] == ["name", "value"]
        assert "line_count" not in parsed
        assert artifact is not None
        assert artifact.metadata.model_dump()["count"] == 1

        tables = await runtime.extract_table("session", attachment.id)
        assert tables["tables"][0]["headers"] == ["name", "value"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_soft_delete_removes_parsed_file_data(tmp_path):
    db = Database(str(tmp_path / "delete.db"))
    await db.connect()
    try:
        await db.sessions.create("session")
        message = Message(
            id="message",
            session_id="session",
            role=MessageRole.USER,
            content="use file",
            timestamp=datetime.now(),
        )
        await db.messages.save(message)
        attachment = await db.files.create_attachment(
            session_id="session", filename="a.txt", mime_type="text/plain",
            size_bytes=1, sha256="2" * 64, storage_key="blobs/22/placeholder",
        )
        await db.files.bind_message("session", message.id, [attachment.id])
        await db.files.replace_chunks(
            attachment.id,
            [FileChunk(id="chunk", attachment_id=attachment.id, ordinal=0, content="secret text")],
        )
        await db.files.put_artifact(attachment.id, "summary", "cache", "secret summary")
        await db.files.replace_code_index(
            attachment.id,
            [{
                "path": "a.py", "language": "python", "name": "f",
                "qualified_name": "f", "kind": "function",
                "start_line": 1, "end_line": 1, "signature": "def f()",
            }],
            [],
        )

        assert await db.files.soft_delete_attachment(attachment.id, "session") is True

        assert await db.files.get_chunks(attachment.id) == []
        assert await db.files.search_chunks(attachment.id, "secret") == []
        assert await db.files.get_artifact("cache") is None
        assert await db.files.find_symbols(attachment.id, "f") == []
        assert await db.files.attachments_for_messages([message.id]) == {}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_read_failed_attachment_is_not_reported_as_waiting(tmp_path):
    db = Database(str(tmp_path / "failed.db"))
    await db.connect()
    try:
        await db.sessions.create("session")
        settings = Settings(
            _env_file=None,
            sqlite_db_path=str(tmp_path / "failed.db"),
            chromadb_path=str(tmp_path / "chroma"),
            file_storage_path=str(tmp_path / "storage"),
        )
        runtime = _make_runtime(db.files, settings)
        attachment = await db.files.create_attachment(
            session_id="session", filename="a.txt", mime_type="text/plain",
            size_bytes=1, sha256="3" * 64, storage_key="blobs/33/placeholder",
        )
        await db.files.update_attachment(attachment.id, status="failed", error_message="parse failed")

        result = await runtime.read_file("session", attachment.id)

        assert result["waiting"] is False
        assert result["error"] == "parse failed"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_empty_ocr_image_summary_does_not_fail_attachment(tmp_path):
    db = Database(str(tmp_path / "image-summary.db"))
    await db.connect()
    try:
        await db.sessions.create("session")
        settings = Settings(
            _env_file=None,
            sqlite_db_path=str(tmp_path / "image-summary.db"),
            chromadb_path=str(tmp_path / "chroma"),
            file_storage_path=str(tmp_path / "storage"),
        )
        runtime = _make_runtime(db.files, settings)
        attachment = await db.files.create_attachment(
            session_id="session", filename="shot.png", mime_type="image/png",
            size_bytes=1, sha256="4" * 64, storage_key="blobs/44/placeholder",
        )
        await db.files.update_attachment(attachment.id, status="ready", adapter_name="image")

        summary = await runtime.summarize_file("session", attachment.id)
        current = await db.files.get_attachment(attachment.id, "session")

        assert summary["no_text"] is True
        assert current and current.status.value == "ready"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_image_adapter_passes_path_to_rapidocr(tmp_path, monkeypatch):
    from PIL import Image

    seen: dict[str, object] = {}

    class FakeRapidOCR:
        def __call__(self, img_content):
            seen["img_content"] = img_content
            return ([[None, "hello"]], None)

    rapidocr = types.ModuleType("rapidocr_onnxruntime")
    rapidocr.RapidOCR = FakeRapidOCR
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", rapidocr)

    image_path = tmp_path / "sample.png"
    Image.new("RGB", (12, 8), "white").save(image_path)

    result = await ImageAdapter().extract(_context(image_path, tmp_path), Settings(_env_file=None))

    assert seen["img_content"] == image_path
    assert result.units[0].content == "hello"
    assert result.metadata["ocr_available"] is True


@pytest.mark.asyncio
async def test_image_analysis_uses_ocr_fallback_without_vision(tmp_path, monkeypatch):
    from PIL import Image

    class FakeRapidOCR:
        def __call__(self, img_content):
            return ([[None, "invoice total 42"]], None)

    rapidocr = types.ModuleType("rapidocr_onnxruntime")
    rapidocr.RapidOCR = FakeRapidOCR
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", rapidocr)

    db = Database(str(tmp_path / "image-analysis.db"))
    await db.connect()
    try:
        await db.sessions.create("session")
        settings = Settings(
            _env_file=None,
            sqlite_db_path=str(tmp_path / "image-analysis.db"),
            chromadb_path=str(tmp_path / "chroma"),
            file_storage_path=str(tmp_path / "storage"),
        )
        runtime = _make_runtime(db.files, settings)
        image_path = tmp_path / "sample.png"
        Image.new("RGB", (12, 8), "white").save(image_path)
        blob = await runtime.storage.save_stream(_chunks(image_path.read_bytes()))
        attachment = await db.files.create_attachment(
            session_id="session", filename="sample.png", mime_type="image/png",
            size_bytes=blob.size_bytes, sha256=blob.sha256, storage_key=blob.storage_key,
        )

        result = await runtime.analyze_file("session", attachment.id, "读取图片里的文字")

        assert result["can_describe_visual_content"] is False
        assert result["analysis_source"] == "ocr"
        assert result["ocr_text"] == "invoice total 42"
        assert "OCR" in result["message"]
    finally:
        await db.close()


def test_pdf_ocr_page_encodes_rendered_image_as_bytes(tmp_path, monkeypatch):
    from PIL import Image

    seen: dict[str, object] = {}

    class FakeRenderedPage:
        def to_pil(self):
            return Image.new("RGB", (2, 2), "white")

    class FakePage:
        def render(self, scale):
            seen["scale"] = scale
            return FakeRenderedPage()

    class FakePdfDocument:
        def __init__(self, path):
            seen["path"] = path

        def __getitem__(self, page_index):
            seen["page_index"] = page_index
            return FakePage()

    class FakeRapidOCR:
        def __call__(self, img_content):
            seen["img_content"] = img_content
            return ([[None, "pdf text"]], None)

    pdfium = types.ModuleType("pypdfium2")
    pdfium.PdfDocument = FakePdfDocument
    rapidocr = types.ModuleType("rapidocr_onnxruntime")
    rapidocr.RapidOCR = FakeRapidOCR
    monkeypatch.setitem(sys.modules, "pypdfium2", pdfium)
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", rapidocr)

    pdf_path = tmp_path / "sample.pdf"
    text = PdfAdapter._ocr_page(pdf_path, 3)

    assert text == "pdf text"
    assert seen["path"] == str(pdf_path)
    assert seen["page_index"] == 3
    assert seen["scale"] == 2
    assert isinstance(seen["img_content"], bytes)
    assert seen["img_content"].startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_file_capability_governance_is_applied_to_runtime_manager(tmp_path):
    db = Database(str(tmp_path / "tools.db"))
    await db.connect()
    try:
        settings = Settings(
            _env_file=None,
            sqlite_db_path=str(tmp_path / "tools.db"),
            chromadb_path=str(tmp_path / "chroma"),
            file_storage_path=str(tmp_path / "storage"),
        )
        runtime = _make_runtime(db.files, settings)
        manager = make_tool_manager()
        catalog = ToolCatalogService(db.tools)
        await ToolRegistry(manager, catalog).install(build_file_tool_specs(runtime))
        await db.tools.update("read_file", enabled=False, risk_level="high", require_approval=True)

        restarted = make_tool_manager()
        await ToolRegistry(restarted, catalog).install(build_file_tool_specs(runtime))

        assert restarted.is_enabled("read_file") is False
        assert restarted.get_risk_level("read_file") == "high"
        assert restarted.require_approval("read_file") is True
    finally:
        await db.close()



def test_adapter_registry_excludes_archive_formats():
    registry = AdapterRegistry()

    assert "archive" not in {adapter.info.name for adapter in registry.list()}
    assert not {".zip", ".tar", ".tgz", ".tar.gz"} & set(registry.supported_extensions())
    with pytest.raises(ValueError, match="不支持的文件类型"):
        registry.select("project.zip", "application/zip")


def test_file_chunks_respect_token_limit(tmp_path):
    settings = Settings(
        _env_file=None,
        file_storage_path=str(tmp_path / "storage"),
        file_chunk_tokens=8,
        file_chunk_overlap_tokens=2,
    )
    runtime = _make_runtime(AsyncMock(), settings)
    units = [
        ExtractedUnit(
            content="这是一段用于验证中文分块边界的长文本内容",
            locator={"path": "notes.txt"},
        )
    ]

    chunks = runtime._chunk_units("file-1", units)

    assert len(chunks) > 1
    assert all(0 < chunk.token_count <= settings.file_chunk_tokens for chunk in chunks)


def test_cache_key_changes_with_every_version_dimension(tmp_path):
    from datetime import datetime
    from athena.models.file import Attachment

    attachment = Attachment(
        id="f", session_id="s", filename="a.txt", mime_type="text/plain",
        size_bytes=1, sha256="a" * 64, storage_key="blobs/aa/hash",
        created_at=datetime.now(), updated_at=datetime.now(),
    )
    base = FileIntelligenceRuntime.cache_key(attachment, "summary", {"kind": "short"}, "1", "m1", "p1")
    assert base != FileIntelligenceRuntime.cache_key(attachment, "summary", {"kind": "short"}, "2", "m1", "p1")
    assert base != FileIntelligenceRuntime.cache_key(attachment, "summary", {"kind": "short"}, "1", "m2", "p1")
    assert base != FileIntelligenceRuntime.cache_key(attachment, "summary", {"kind": "short"}, "1", "m1", "p2")


@pytest.mark.asyncio
async def test_office_pdf_and_image_adapters_preserve_structure(tmp_path):
    from docx import Document
    from openpyxl import Workbook
    from PIL import Image
    from pypdf import PdfWriter

    settings = Settings(_env_file=None)

    embedded_image_path = tmp_path / "embedded.png"
    Image.new("RGB", (12, 8), "white").save(embedded_image_path)

    text_path = tmp_path / "sample.txt"
    text_path.write_text("hello\nworld\n", encoding="utf-8")
    text = await TextAdapter().extract(_context(text_path, tmp_path), settings)
    assert text.units[0].metadata == {"kind": "text", "format": "txt"}

    csv_path = tmp_path / "sample.csv"
    csv_path.write_text("name,value\nalpha,1\n", encoding="utf-8")
    csv = await TextAdapter().extract(_context(csv_path, tmp_path), settings)
    assert csv.units[0].metadata == {"kind": "table", "format": "csv"}

    document = Document()
    document.add_heading("Title", level=1)
    document.add_paragraph("Body")
    document.add_picture(str(embedded_image_path))
    docx_path = tmp_path / "sample.docx"
    document.save(docx_path)
    word = await WordAdapter().extract(_context(docx_path, tmp_path), settings)
    assert word.units[0].metadata["style"] == "Heading 1"
    assert word.metadata["images"] == 1
    assert any(unit.metadata.get("kind") == "image" for unit in word.units)

    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = 2
    sheet["A2"] = "=A1*2"
    xlsx_path = tmp_path / "sample.xlsx"
    workbook.save(xlsx_path)
    workbook.close()
    excel = await ExcelAdapter().extract(_context(xlsx_path, tmp_path), settings)
    assert excel.metadata["sheets"][0]["formulas"] == [{"cell": "A2", "formula": "=A1*2"}]

    image_path = tmp_path / "sample.png"
    Image.new("RGB", (12, 8), "white").save(image_path)
    image = await ImageAdapter().extract(_context(image_path, tmp_path), settings)
    assert (image.metadata["width"], image.metadata["height"]) == (12, 8)

    pdf_path = tmp_path / "sample.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with pdf_path.open("wb") as output:
        writer.write(output)
    pdf = await PdfAdapter().extract(_context(pdf_path, tmp_path), settings)
    assert pdf.metadata["pages"] == 1
