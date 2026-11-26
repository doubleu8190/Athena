"""M3.1 会话附件 PDF / Office 全解析、提取缓存、工具运行时与安全预览端到端测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import sqlite3
import zipfile
from collections.abc import AsyncIterator, Sequence
from contextlib import redirect_stdout
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from auth_support import AuthTestClient, make_auth_service
from docx import Document
from openpyxl import Workbook  # type: ignore[import-untyped]
from pg_unit_harness import pg_unit_db
from PIL import Image
from pptx import Presentation
from pptx.util import Inches
from sqlalchemy import text

from adapter.auth.model import UserModel
from adapter.file.repository import (
    SqlAlchemySessionFileRepository,
    pending_file_to_model,
)
from adapter.file.storage import (
    _ORPHAN_KEY,
    FileStorageError,
    MinioFileObjectStore,
    MinioFileStorageConfig,
)
from adapter.session.model import SessionModel
from app.file import FileInputError, FileObjectMaintenance, SessionFileService
from app.run.context import messages_to_llm
from app.run.safe_stop import SafeStopReporter
from app.tool.runtime import (
    SESSION_FILE_READ_DEFINITION,
    RunToolRuntimeService,
)
from bootstrap.api import build_api_dependencies
from bootstrap.run_worker import RunWorkerBundle, build_run_worker_dependencies
from domain.auth import User, UserRole, UserStatus
from domain.file import (
    FileObjectScanEntry,
    PendingSessionFile,
    SessionDocumentExtractionResult,
    SessionFile,
    SessionFileExtractionStatus,
    SessionFilePage,
    SessionFileSource,
    TextFileReadError,
)
from domain.knowledge.model import ChunkLocator, TextBlock
from domain.knowledge.ports import ParsedDocument
from domain.run.model import (
    ContextStopReason,
    Run,
    RunInputFileContext,
    RunInputFiles,
    RunStatus,
    RunUsage,
)
from domain.session import Session, SessionNotFoundError, SessionScope
from domain.tool import ToolExecutionContext, ToolOutcomeStatus
from infrastructure.file import LocalSessionDocumentExtractor
from infrastructure.knowledge.parsers import LocalDocumentParser
from main import create_app

NOW = datetime(2026, 11, 24, tzinfo=UTC)
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


# ---------------------------------------------------------------------------
# 真实 PDF / DOCX / XLSX / PPTX / 位图生成夹具
# ---------------------------------------------------------------------------


def _build_multipage_pdf(pages: Sequence[str]) -> bytes:
    """生成带真实文本层的最小合法多页 PDF 文档。"""
    assert pages, "at least one page required"
    objects: list[bytes] = []
    # 1: Catalog, 2: Pages, 3: Font
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    page_obj_nums = [4 + i * 2 for i in range(len(pages))]
    kids = " ".join(f"{num} 0 R" for num in page_obj_nums)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for idx, page_text in enumerate(pages):
        content_obj_num = 4 + idx * 2 + 1
        escaped = page_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream_str = f"BT /F1 12 Tf 40 760 Td ({escaped}) Tj ET"
        stream_bytes = stream_str.encode("latin-1")
        page_dict = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_obj_num} 0 R >>"
        ).encode()
        content_dict = (
            f"<< /Length {len(stream_bytes)} >>\nstream\n".encode() + stream_bytes + b"\nendstream"
        )
        objects.append(page_dict)
        objects.append(content_dict)

    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    )
    return bytes(output)


def _build_blank_pdf() -> bytes:
    """生成不含任何文本流的空白 PDF（模拟无文本层扫描件）。"""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    )
    return bytes(output)


def _build_scanned_image_pdf() -> bytes:
    """生成仅含纯图片无文本层的 PDF 文档。"""
    buf = io.BytesIO()
    Image.new("RGB", (200, 100), color="white").save(buf, format="PDF")
    return buf.getvalue()


def _build_docx_bytes(*, include_image: bool = False) -> bytes:
    doc = Document()
    doc.add_heading("季度业务复盘", level=1)
    doc.add_paragraph("华东大区销售额同比增长 28%，客户留存稳步提升。")
    doc.add_heading("核心数据表", level=2)
    doc.add_paragraph("以下为各区域明细：")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "区域"
    table.cell(0, 1).text = "营收"
    table.cell(1, 0).text = "华东"
    table.cell(1, 1).text = "1280万"
    if include_image:
        img_buf = io.BytesIO()
        Image.new("RGB", (64, 64), color="blue").save(img_buf, format="PNG")
        img_buf.seek(0)
        doc.add_picture(img_buf)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _build_xlsx_bytes() -> bytes:
    wb = Workbook()
    ws1 = wb.active
    assert ws1 is not None
    ws1.title = "销售明细"
    ws1["A1"] = "区域"
    ws1["B1"] = "金额"
    ws1["A2"] = "华东"
    ws1["B2"] = 1280
    ws2 = wb.create_sheet("年度汇总")
    ws2["A1"] = "指标"
    ws2["B1"] = "数值"
    ws2["A2"] = "总营收"
    ws2["B2"] = 5600
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_pptx_bytes(*, include_picture: bool = False) -> bytes:
    prs = Presentation()
    slide1 = prs.slides.add_slide(prs.slide_layouts[1])
    assert slide1.shapes.title is not None
    slide1.shapes.title.text = "2026 第四季度规划"
    slide1.placeholders[1].text = "聚焦企业知识库与会话文档分析"
    if include_picture:
        img_buf = io.BytesIO()
        Image.new("RGB", (64, 64), color="green").save(img_buf, format="PNG")
        img_buf.seek(0)
        slide1.shapes.add_picture(img_buf, Inches(1), Inches(1), Inches(1), Inches(1))
    slide2 = prs.slides.add_slide(prs.slide_layouts[5])
    table_shape = slide2.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(4), Inches(1.5))
    table = table_shape.table
    table.cell(0, 0).text = "里程碑"
    table.cell(0, 1).text = "状态"
    table.cell(1, 0).text = "M3.1 后端"
    table.cell(1, 1).text = "已完成"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _build_image_bytes(fmt: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color="red").save(buf, format=fmt)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 内存对象存储与内存仓储替身
# ---------------------------------------------------------------------------


class _MemoryObjectStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.puts: list[str] = []
        self.deletes: list[str] = []

    async def start(self) -> None:
        return None

    async def put_file(self, key: str, path: Path, *, content_type: str) -> None:
        self.puts.append(key)
        self.values[key] = path.read_bytes()
        self.content_types[key] = content_type

    async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
        self.puts.append(key)
        self.values[key] = content
        self.content_types[key] = content_type

    async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
        if key not in self.values:
            raise FileStorageError("file_storage_not_found")
        data = self.values[key]
        if len(data) > maximum_bytes:
            raise FileStorageError("file_storage_object_too_large")
        return data

    async def get_range(self, key: str, *, offset: int, length: int) -> bytes:
        if key not in self.values:
            raise FileStorageError("file_storage_not_found")
        return self.values[key][offset : offset + length]

    async def iter_range(
        self,
        key: str,
        *,
        offset: int = 0,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        if key not in self.values:
            raise FileStorageError("file_storage_not_found")
        content = self.values[key]
        for start in range(offset, len(content), chunk_size):
            yield content[start : start + chunk_size]

    async def delete(self, key: str) -> None:
        self.deletes.append(key)
        self.values.pop(key, None)

    async def iter_maintenance_candidates(
        self,
        *,
        older_than_seconds: int = 86400,
        after_key: str | None = None,
        maximum_items: int = 1000,
    ) -> AsyncIterator[FileObjectScanEntry]:
        del older_than_seconds, after_key, maximum_items
        if False:
            yield FileObjectScanEntry("session-files/00/empty", False)

    async def aclose(self) -> None:
        return None


class _MemorySessionFileRepository:
    def __init__(self) -> None:
        self.files: dict[str, SessionFile] = {}
        self.idempotency: dict[tuple[str, str, str], tuple[str, SessionFile]] = {}
        self.update_calls: int = 0

    async def publish_upload(
        self,
        session_id: str,
        owner_id: str,
        file: PendingSessionFile,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        published_at: datetime,
    ) -> SessionFile:
        del published_at
        key = (session_id, owner_id, idempotency_key)
        existing = self.idempotency.get(key)
        if existing is not None:
            fp, record = existing
            if fp != request_fingerprint:
                raise ValueError("idempotency_conflict")
            return record
        record = SessionFile(
            id=file.id,
            session_id=session_id,
            owner_id=owner_id,
            source=SessionFileSource.UPLOAD,
            filename=file.filename,
            label=file.label,
            mime_type=file.mime_type,
            size_bytes=file.size_bytes,
            sha256=file.sha256,
            object_key=file.object_key,
            source_call_id=None,
            output_index=None,
            created_at=file.created_at,
            extraction_status=file.extraction_status,
            extracted_text_key=file.extracted_text_key,
            extracted_text_bytes=file.extracted_text_bytes,
            extraction_error_code=file.extraction_error_code,
        )
        self.idempotency[key] = (request_fingerprint, record)
        self.files[record.id] = record
        return record

    async def find_upload_request(
        self,
        session_id: str,
        owner_id: str,
        idempotency_key: str,
    ) -> SessionFile | None:
        found = self.idempotency.get((session_id, owner_id, idempotency_key))
        return None if found is None else found[1]

    async def list_visible(
        self,
        session_id: str,
        owner_id: str,
        *,
        after_file_id: str | None,
        limit: int,
    ) -> SessionFilePage | None:
        del after_file_id
        items = tuple(
            item
            for item in self.files.values()
            if item.session_id == session_id and item.owner_id == owner_id
        )
        return SessionFilePage(items=items[:limit], next_after_file_id=None)

    async def get_visible(
        self,
        session_id: str,
        owner_id: str,
        file_id: str,
    ) -> SessionFile | None:
        item = self.files.get(file_id)
        if item is None or item.session_id != session_id or item.owner_id != owner_id:
            return None
        return item

    async def get_visible_many(
        self,
        session_id: str,
        owner_id: str,
        file_ids: tuple[str, ...],
    ) -> tuple[SessionFile, ...]:
        if len(file_ids) != len(set(file_ids)):
            raise ValueError("session_file_ids_duplicate")
        items: list[SessionFile] = []
        for file_id in file_ids:
            found = await self.get_visible(session_id, owner_id, file_id)
            if found is None:
                raise ValueError("session_file_not_found")
            items.append(found)
        return tuple(items)

    async def update_extraction(
        self,
        file_id: str,
        *,
        extraction_status: SessionFileExtractionStatus,
        extracted_text_key: str | None,
        extracted_text_bytes: int | None,
        extraction_error_code: str | None,
    ) -> SessionFile | None:
        self.update_calls += 1
        existing = self.files.get(file_id)
        if existing is None:
            return None
        if existing.extraction_status is SessionFileExtractionStatus.READY:
            return existing
        updated = replace(
            existing,
            extraction_status=extraction_status,
            extracted_text_key=extracted_text_key,
            extracted_text_bytes=extracted_text_bytes,
            extraction_error_code=extraction_error_code,
        )
        self.files[file_id] = updated
        return updated


# ---------------------------------------------------------------------------
# 1. LocalDocumentParser(enable_ocr=False) 与 LocalSessionDocumentExtractor 测试
# ---------------------------------------------------------------------------


def test_local_document_parser_enable_ocr_false_never_invokes_ocr() -> None:
    """enable_ocr=False 时 PDF/DOCX/XLSX/PPTX 即使含嵌入式图片或短文本页也跳过 OCR。"""

    class _ForbiddenOcr:
        def predict(self, **_: Any) -> list[Any]:
            raise AssertionError("OCR engine must not be invoked when enable_ocr=False")

    parser = LocalDocumentParser(ocr_engine=_ForbiddenOcr(), enable_ocr=False)

    pdf_doc = asyncio.run(parser.parse("short.pdf", _build_multipage_pdf(["Hi"])))
    assert len(pdf_doc.blocks) == 1
    assert pdf_doc.blocks[0].text == "Hi"

    docx_doc = asyncio.run(parser.parse("with-img.docx", _build_docx_bytes(include_image=True)))
    assert any("季度业务复盘" in block.text for block in docx_doc.blocks)

    pptx_doc = asyncio.run(parser.parse("with-pic.pptx", _build_pptx_bytes(include_picture=True)))
    assert any("2026 第四季度规划" in block.text for block in pptx_doc.blocks)

    img_doc = asyncio.run(parser.parse("pic.png", _build_image_bytes("PNG")))
    assert img_doc.blocks == ()


def test_document_extractor_extracts_structured_headers_for_all_four_formats(
    tmp_path: Path,
) -> None:
    """验证真实 PDF / DOCX / XLSX / PPTX 均输出带分节标头的结构化 UTF-8 文本。"""
    extractor = LocalSessionDocumentExtractor()

    pdf_path = tmp_path / "quarterly.pdf"
    pdf_path.write_bytes(_build_multipage_pdf(["Page One Summary", "Page Two Financial Notes"]))
    pdf_res = asyncio.run(extractor.extract(pdf_path.name, pdf_path))
    assert pdf_res.status is SessionFileExtractionStatus.READY
    assert pdf_res.text is not None
    assert "## 第 1 页\nPage One Summary" in pdf_res.text
    assert "## 第 2 页\nPage Two Financial Notes" in pdf_res.text

    docx_path = tmp_path / "review.docx"
    docx_path.write_bytes(_build_docx_bytes())
    docx_res = asyncio.run(extractor.extract(docx_path.name, docx_path))
    assert docx_res.status is SessionFileExtractionStatus.READY
    assert docx_res.text is not None
    assert "## Heading 1\n季度业务复盘" in docx_res.text
    assert "华东大区销售额同比增长 28%" in docx_res.text
    assert "## table 1\n区域 | 营收\n华东 | 1280万" in docx_res.text
    assert "## Normal" not in docx_res.text

    xlsx_path = tmp_path / "sales.xlsx"
    xlsx_path.write_bytes(_build_xlsx_bytes())
    xlsx_res = asyncio.run(extractor.extract(xlsx_path.name, xlsx_path))
    assert xlsx_res.status is SessionFileExtractionStatus.READY
    assert xlsx_res.text is not None
    assert "## 工作表：销售明细" in xlsx_res.text
    assert "A2: 华东 | B2: 1280" in xlsx_res.text
    assert "## 工作表：年度汇总" in xlsx_res.text
    assert "A2: 总营收 | B2: 5600" in xlsx_res.text

    pptx_path = tmp_path / "deck.pptx"
    pptx_path.write_bytes(_build_pptx_bytes())
    pptx_res = asyncio.run(extractor.extract(pptx_path.name, pptx_path))
    assert pptx_res.status is SessionFileExtractionStatus.READY
    assert pptx_res.text is not None
    assert "## 幻灯片 1" in pptx_res.text
    assert "2026 第四季度规划" in pptx_res.text
    assert "## 幻灯片 2" in pptx_res.text
    assert "M3.1 后端 | 已完成" in pptx_res.text


def test_document_extractor_security_boundaries_and_safe_error_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证无文本层、损坏、加密、宏、超页数、超体积、超文本上限与超时全部返回稳定错误码。"""
    extractor = LocalSessionDocumentExtractor()

    # 1. 空白 PDF 与纯图片扫描 PDF -> document_no_extractable_text
    blank_pdf = tmp_path / "blank.pdf"
    blank_pdf.write_bytes(_build_blank_pdf())
    res_blank = asyncio.run(extractor.extract(blank_pdf.name, blank_pdf))
    assert res_blank == SessionDocumentExtractionResult.failed("document_no_extractable_text")

    scan_pdf = tmp_path / "scan.pdf"
    scan_pdf.write_bytes(_build_scanned_image_pdf())
    res_scan = asyncio.run(extractor.extract(scan_pdf.name, scan_pdf))
    assert res_scan == SessionDocumentExtractionResult.failed("document_no_extractable_text")

    # 2. 损坏 PDF -> document_parse_failed（绝不泄露宿主路径或堆栈）
    corrupt_pdf = tmp_path / "corrupt.pdf"
    corrupt_pdf.write_bytes(b"%PDF-1.7\nthis-is-corrupted-data")
    res_corrupt = asyncio.run(extractor.extract(corrupt_pdf.name, corrupt_pdf))
    assert res_corrupt == SessionDocumentExtractionResult.failed("document_parse_failed")
    assert str(tmp_path) not in (res_corrupt.error_code or "")

    # 3. 带宏 Office 文档 -> document_macro_unsupported
    macro_docx = tmp_path / "macro.docx"
    with zipfile.ZipFile(macro_docx, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("word/document.xml", b"<w:document/>")
        archive.writestr("word/vbaProject.bin", b"binary-macro")
    res_macro = asyncio.run(extractor.extract(macro_docx.name, macro_docx))
    assert res_macro == SessionDocumentExtractionResult.failed("document_macro_unsupported")

    # 4. 加密 Office ZIP（局部头与中央目录均置加密位 0x1）-> document_encrypted
    encrypted_docx = tmp_path / "encrypted.docx"
    encrypted_docx.write_bytes(_build_docx_bytes())
    raw_zip = bytearray(encrypted_docx.read_bytes())
    local_header = raw_zip.find(b"PK\x03\x04")
    central_header = raw_zip.find(b"PK\x01\x02")
    assert local_header >= 0 and central_header >= 0
    raw_zip[local_header + 6] |= 1
    raw_zip[central_header + 8] |= 1
    encrypted_docx.write_bytes(raw_zip)
    res_enc_docx = asyncio.run(extractor.extract(encrypted_docx.name, encrypted_docx))
    assert res_enc_docx == SessionDocumentExtractionResult.failed("document_encrypted")

    # 5. 密码保护 PDF（err_code == 4）-> document_encrypted
    class _FakePasswordError(Exception):
        err_code = 4

    pw_parser = LocalDocumentParser(enable_ocr=False)

    def _raise_password(*_args: object, **_kwargs: object) -> None:
        raise ValueError("knowledge_pdf_invalid") from _FakePasswordError("password required")

    monkeypatch.setattr(pw_parser, "validate_file", _raise_password)
    pw_extractor = LocalSessionDocumentExtractor(parser=pw_parser)
    res_pw_pdf = asyncio.run(pw_extractor.extract(blank_pdf.name, blank_pdf))
    assert res_pw_pdf == SessionDocumentExtractionResult.failed("document_encrypted")

    # 5b. PDF 页数超限（>500）-> document_page_limit_exceeded
    page_limit_parser = LocalDocumentParser(enable_ocr=False)

    def _raise_page_limit(*_args: object, **_kwargs: object) -> None:
        raise ValueError("knowledge_pdf_page_limit")

    monkeypatch.setattr(page_limit_parser, "validate_file", _raise_page_limit)
    page_limit_extractor = LocalSessionDocumentExtractor(parser=page_limit_parser)
    res_page_limit = asyncio.run(page_limit_extractor.extract(blank_pdf.name, blank_pdf))
    assert res_page_limit == SessionDocumentExtractionResult.failed("document_page_limit_exceeded")

    # 6. 单文件大小超限 -> document_extract_too_large
    tiny_limit_extractor = LocalSessionDocumentExtractor(max_file_bytes=32)
    valid_pdf = tmp_path / "valid.pdf"
    valid_pdf.write_bytes(_build_multipage_pdf(["Hello Athena"]))
    res_too_large = asyncio.run(tiny_limit_extractor.extract(valid_pdf.name, valid_pdf))
    assert res_too_large == SessionDocumentExtractionResult.failed("document_extract_too_large")

    # 7. 提取文本超过 UTF-8 字节上限 -> document_extracted_text_too_large
    tiny_text_extractor = LocalSessionDocumentExtractor(max_text_bytes=16)
    res_text_limit = asyncio.run(tiny_text_extractor.extract(valid_pdf.name, valid_pdf))
    assert res_text_limit == SessionDocumentExtractionResult.failed(
        "document_extracted_text_too_large"
    )

    # 8. 超时降级 -> document_extract_timeout
    slow_parser = LocalDocumentParser(enable_ocr=False)

    def _slow_parse(_filename: str, _content: bytes) -> ParsedDocument:
        import time

        time.sleep(0.25)
        return ParsedDocument((TextBlock("slow", ChunkLocator(kind="pdf", page=1)),))

    monkeypatch.setattr(slow_parser, "parse_sync", _slow_parse)
    timeout_extractor = LocalSessionDocumentExtractor(parser=slow_parser, timeout_seconds=0.05)
    res_timeout = asyncio.run(timeout_extractor.extract(valid_pdf.name, valid_pdf))
    assert res_timeout == SessionDocumentExtractionResult.failed("document_extract_timeout")


# ---------------------------------------------------------------------------
# 2. SessionFileService 上传缓存、分页边界、降级、惰性解析与孤儿清理测试
# ---------------------------------------------------------------------------


def test_session_file_service_upload_extracts_caches_and_paginates_utf8(tmp_path: Path) -> None:
    """上传 PDF/Office 后自动写提取缓存，幂等重放不重复解析，分页严格遵守 UTF-8 边界。"""
    objects = _MemoryObjectStore()
    repository = _MemorySessionFileRepository()

    class _CountingExtractor:
        def __init__(self) -> None:
            self.inner = LocalSessionDocumentExtractor()
            self.calls = 0

        async def extract(self, filename: str, path: Path) -> SessionDocumentExtractionResult:
            self.calls += 1
            return await self.inner.extract(filename, path)

    extractor = _CountingExtractor()
    ids = iter(("file_docx_1", "file_pdf_2"))
    service = SessionFileService(
        repository,
        objects,
        document_extractor=extractor,
        id_factory=lambda _: next(ids),
        clock=lambda: NOW,
    )

    docx_path = tmp_path / "report.docx"
    docx_path.write_bytes(_build_docx_bytes())

    async def scenario() -> None:
        uploaded = await service.upload(
            "ses_1",
            "usr_1",
            idempotency_key="idem-docx-1",
            filename="report.docx",
            declared_mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            path=docx_path,
        )
        assert uploaded.extraction_status is SessionFileExtractionStatus.READY
        assert uploaded.extracted_text_key == (
            f"session-files-extracted/{uploaded.sha256[:2]}/file_docx_1.txt"
        )
        assert uploaded.extracted_text_bytes is not None and uploaded.extracted_text_bytes > 0
        assert uploaded.extraction_error_code is None
        assert extractor.calls == 1
        assert objects.content_types[uploaded.extracted_text_key] == "text/plain; charset=utf-8"

        # 幂等重放直接命中原记录，绝不重复解析或重复写对象。
        replayed = await service.upload(
            "ses_1",
            "usr_1",
            idempotency_key="idem-docx-1",
            filename="report.docx",
            declared_mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            path=docx_path,
        )
        assert replayed == uploaded
        assert extractor.calls == 1

        # 按极小字节窗口（13 字节）分页遍历含大量中文字符的提取缓存，验证多字节边界完整还原。
        collected: list[str] = []
        offset = 0
        steps = 0
        while True:
            page = await service.read_text(
                "ses_1",
                "usr_1",
                uploaded.id,
                offset=offset,
                limit_bytes=13,
            )
            assert page.source_kind == "extracted_document"
            assert page.offset == offset
            collected.append(page.content)
            offset = page.next_offset
            steps += 1
            if page.eof:
                assert page.truncated is False
                break
            assert page.truncated is True
        assert steps > 3
        full_text = "".join(collected)
        expected_text = objects.values[uploaded.extracted_text_key].decode("utf-8")
        assert full_text == expected_text
        assert "## Heading 1\n季度业务复盘" in full_text

        # 找到首个中文字符字节位置，验证续字节偏移报 file_offset_invalid，
        # 过小窗口报 file_range_too_small。
        raw_utf8 = objects.values[uploaded.extracted_text_key]
        chinese_pos = next(i for i, b in enumerate(raw_utf8) if b >= 0xE0)
        with pytest.raises(TextFileReadError, match="file_offset_invalid"):
            await service.read_text(
                "ses_1",
                "usr_1",
                uploaded.id,
                offset=chinese_pos + 1,
                limit_bytes=8,
            )
        with pytest.raises(TextFileReadError, match="file_range_too_small"):
            await service.read_text(
                "ses_1",
                "usr_1",
                uploaded.id,
                offset=chinese_pos,
                limit_bytes=1,
            )

    asyncio.run(scenario())


def test_upload_degrades_gracefully_on_extraction_failure_without_blocking_original(
    tmp_path: Path,
) -> None:
    """空白 PDF、损坏 PDF 与超时降级均不阻断原件上传，准确落库 failed 及稳定错误码。"""
    objects = _MemoryObjectStore()
    repository = _MemorySessionFileRepository()

    class _TimeoutExtractor:
        async def extract(self, filename: str, path: Path) -> SessionDocumentExtractionResult:
            del filename, path
            return SessionDocumentExtractionResult.failed("document_extract_timeout")

    blank_path = tmp_path / "blank.pdf"
    blank_path.write_bytes(_build_blank_pdf())
    corrupt_path = tmp_path / "corrupt.pdf"
    corrupt_path.write_bytes(b"%PDF-1.7\nbroken-body")
    valid_path = tmp_path / "valid.pdf"
    valid_path.write_bytes(_build_multipage_pdf(["Some text"]))

    ids = iter(("file_blank", "file_corrupt", "file_timeout"))
    real_service = SessionFileService(
        repository,
        objects,
        document_extractor=LocalSessionDocumentExtractor(),
        id_factory=lambda _: next(ids),
        clock=lambda: NOW,
    )
    timeout_service = SessionFileService(
        repository,
        objects,
        document_extractor=_TimeoutExtractor(),
        id_factory=lambda _: next(ids),
        clock=lambda: NOW,
    )

    async def scenario() -> None:
        f_blank = await real_service.upload(
            "ses_1",
            "usr_1",
            idempotency_key="k-blank",
            filename="blank.pdf",
            declared_mime="application/pdf",
            path=blank_path,
        )
        assert f_blank.extraction_status is SessionFileExtractionStatus.FAILED
        assert f_blank.extraction_error_code == "document_no_extractable_text"
        assert f_blank.extracted_text_key is None
        assert f_blank.extracted_text_bytes is None
        assert f_blank.object_key in objects.values

        f_corrupt = await real_service.upload(
            "ses_1",
            "usr_1",
            idempotency_key="k-corrupt",
            filename="corrupt.pdf",
            declared_mime="application/pdf",
            path=corrupt_path,
        )
        assert f_corrupt.extraction_status is SessionFileExtractionStatus.FAILED
        assert f_corrupt.extraction_error_code == "document_parse_failed"
        assert f_corrupt.object_key in objects.values

        f_timeout = await timeout_service.upload(
            "ses_1",
            "usr_1",
            idempotency_key="k-timeout",
            filename="valid.pdf",
            declared_mime="application/pdf",
            path=valid_path,
        )
        assert f_timeout.extraction_status is SessionFileExtractionStatus.FAILED
        assert f_timeout.extraction_error_code == "document_extract_timeout"
        assert f_timeout.object_key in objects.values

        # 不应有任何 session-files-extracted/ 对象被写入。
        assert not any(k.startswith("session-files-extracted/") for k in objects.values)

        with pytest.raises(TextFileReadError) as err:
            await real_service.read_text("ses_1", "usr_1", f_blank.id, offset=0, limit_bytes=1024)
        assert err.value.code == "document_no_extractable_text"

    asyncio.run(scenario())


def test_lazy_extraction_backfills_legacy_and_artifact_documents_and_cleans_race_orphan() -> None:
    """历史 none 文档与 Skill 产物首次 read_text 惰性解析回填缓存，并发冲突清理多余缓存对象。"""
    objects = _MemoryObjectStore()
    repository = _MemorySessionFileRepository()

    class _CountingExtractor:
        def __init__(self) -> None:
            self.inner = LocalSessionDocumentExtractor()
            self.calls = 0

        async def extract(self, filename: str, path: Path) -> SessionDocumentExtractionResult:
            self.calls += 1
            return await self.inner.extract(filename, path)

    extractor = _CountingExtractor()
    service = SessionFileService(
        repository,
        objects,
        document_extractor=extractor,
        clock=lambda: NOW,
    )

    # 1. 历史上传 PDF（extraction_status="none"）
    pdf_bytes = _build_multipage_pdf(["Legacy PDF Page 1", "Legacy PDF Page 2"])
    legacy_key = "session-files/ab/file_legacy_pdf"
    objects.values[legacy_key] = pdf_bytes
    legacy_file = SessionFile(
        id="file_legacy_pdf",
        session_id="ses_1",
        owner_id="usr_1",
        source=SessionFileSource.UPLOAD,
        filename="legacy.pdf",
        label=None,
        mime_type="application/pdf",
        size_bytes=len(pdf_bytes),
        sha256="ab" * 32,
        object_key=legacy_key,
        source_call_id=None,
        output_index=None,
        created_at=NOW,
    )
    repository.files[legacy_file.id] = legacy_file

    # 2. Skill 生成的 XLSX 产物（source=ARTIFACT, extraction_status="none"）
    xlsx_bytes = _build_xlsx_bytes()
    artifact_key = f"session-artifacts/cd/{'cd' * 32}/file_art_xlsx"
    objects.values[artifact_key] = xlsx_bytes
    artifact_file = SessionFile(
        id="file_art_xlsx",
        session_id="ses_1",
        owner_id="usr_1",
        source=SessionFileSource.ARTIFACT,
        filename="generated.xlsx",
        label="月度报表",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=len(xlsx_bytes),
        sha256="cd" * 32,
        object_key=artifact_key,
        source_call_id="call_1",
        output_index=0,
        created_at=NOW,
    )
    repository.files[artifact_file.id] = artifact_file

    async def scenario() -> None:
        # 首次读取历史 PDF -> 触发惰性提取并回填缓存
        first = await service.read_text(
            "ses_1",
            "usr_1",
            legacy_file.id,
            offset=0,
            limit_bytes=4096,
        )
        assert first.source_kind == "extracted_document"
        assert first.file.extraction_status is SessionFileExtractionStatus.READY
        assert "## 第 1 页\nLegacy PDF Page 1" in first.content
        assert "## 第 2 页\nLegacy PDF Page 2" in first.content
        assert extractor.calls == 1

        # 二次读取直接命中缓存，不重复提取
        second = await service.read_text(
            "ses_1",
            "usr_1",
            legacy_file.id,
            offset=0,
            limit_bytes=4096,
        )
        assert second.content == first.content
        assert extractor.calls == 1

        # 首次读取 Skill 生成的 XLSX 产物 -> 同样惰性解析回填
        art_slice = await service.read_text(
            "ses_1",
            "usr_1",
            artifact_file.id,
            offset=0,
            limit_bytes=4096,
        )
        assert art_slice.source_kind == "extracted_document"
        assert "## 工作表：销售明细" in art_slice.content
        assert extractor.calls == 2

    asyncio.run(scenario())

    # 3. 并发竞争场景：update_extraction 返回并发胜者已提交的不同缓存键，
    # 败者必须清理自己多余对象并用胜者键读取。
    winner_key = "session-files-extracted/ef/winner_file.txt"
    winner_payload = "## 第 1 页\n并发胜者缓存".encode()
    objects.values[winner_key] = winner_payload

    class _RaceRepository(_MemorySessionFileRepository):
        async def update_extraction(
            self,
            file_id: str,
            *,
            extraction_status: SessionFileExtractionStatus,
            extracted_text_key: str | None,
            extracted_text_bytes: int | None,
            extraction_error_code: str | None,
        ) -> SessionFile | None:
            del extraction_status, extracted_text_key, extracted_text_bytes, extraction_error_code
            existing = self.files[file_id]
            committed = replace(
                existing,
                extraction_status=SessionFileExtractionStatus.READY,
                extracted_text_key=winner_key,
                extracted_text_bytes=len(winner_payload),
                extraction_error_code=None,
            )
            self.files[file_id] = committed
            return committed

    race_repo = _RaceRepository()
    race_file = replace(
        legacy_file,
        id="file_race_pdf",
        sha256="ef" * 32,
        extraction_status=SessionFileExtractionStatus.NONE,
        extracted_text_key=None,
        extracted_text_bytes=None,
    )
    race_repo.files[race_file.id] = race_file
    race_service = SessionFileService(
        race_repo,
        objects,
        document_extractor=LocalSessionDocumentExtractor(),
        id_factory=lambda prefix: f"{prefix}_race_loser",
        clock=lambda: NOW,
    )

    race_slice = asyncio.run(
        race_service.read_text("ses_1", "usr_1", race_file.id, offset=0, limit_bytes=1024)
    )
    loser_key = "session-files-extracted/ef/extract_race_loser.txt"
    assert loser_key in objects.deletes
    assert loser_key not in objects.values
    assert winner_key in objects.values
    assert race_slice.content == "## 第 1 页\n并发胜者缓存"


def test_concurrent_lazy_extraction_cancellation_and_race_never_delete_winner_cache() -> None:
    """真实双协程并发惰性提取：取消、写异常或竞态落败均只删自身尝试键，绝不误删胜者缓存。"""
    pdf_bytes = _build_multipage_pdf(["Concurrent Lazy Extraction Page 1"])
    orig_key = "session-files/ab/file_concurrent_pdf"

    def _make_legacy_file(file_id: str) -> SessionFile:
        return SessionFile(
            id=file_id,
            session_id="ses_1",
            owner_id="usr_1",
            source=SessionFileSource.UPLOAD,
            filename="concurrent.pdf",
            label=None,
            mime_type="application/pdf",
            size_bytes=len(pdf_bytes),
            sha256="ab" * 32,
            object_key=orig_key,
            source_call_id=None,
            output_index=None,
            created_at=NOW,
        )

    class _TwoReaderBarrierRepo(_MemorySessionFileRepository):
        """确保两个并发协程均观测到 extraction_status=none 后再进入惰性提取。"""

        def __init__(self) -> None:
            super().__init__()
            self._none_reads = 0
            self._both_observed_none = asyncio.Event()

        async def get_visible(
            self,
            session_id: str,
            owner_id: str,
            file_id: str,
        ) -> SessionFile | None:
            item = await super().get_visible(session_id, owner_id, file_id)
            if item is not None and item.extraction_status is SessionFileExtractionStatus.NONE:
                self._none_reads += 1
                if self._none_reads >= 2:
                    self._both_observed_none.set()
                await asyncio.wait_for(self._both_observed_none.wait(), timeout=5.0)
            return item

    # ------------------------------------------------------------------
    # 场景 A1：协程 A 胜出提交 ready，协程 B 在 put_bytes 阶段遭遇 CancelledError
    # ------------------------------------------------------------------
    async def scenario_a_cancel() -> None:
        a_committed = asyncio.Event()
        b_entered_put = asyncio.Event()
        loser_task_holder: list[asyncio.Task[Any]] = []

        class _CoordinatedStore(_MemoryObjectStore):
            async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
                if key.endswith("/extract_attempt_b.txt"):
                    # 记录实际拿到 attempt_b 的并发协程，等待胜者提交 ready 后再挂起等待被取消。
                    current = asyncio.current_task()
                    assert current is not None
                    loser_task_holder.append(current)
                    b_entered_put.set()
                    await a_committed.wait()
                    await asyncio.sleep(10)
                await super().put_bytes(key, content, content_type=content_type)

        class _CoordinatedRepo(_TwoReaderBarrierRepo):
            async def update_extraction(
                self,
                file_id: str,
                *,
                extraction_status: SessionFileExtractionStatus,
                extracted_text_key: str | None,
                extracted_text_bytes: int | None,
                extraction_error_code: str | None,
            ) -> SessionFile | None:
                result = await super().update_extraction(
                    file_id,
                    extraction_status=extraction_status,
                    extracted_text_key=extracted_text_key,
                    extracted_text_bytes=extracted_text_bytes,
                    extraction_error_code=extraction_error_code,
                )
                a_committed.set()
                return result

        store = _CoordinatedStore()
        store.values[orig_key] = pdf_bytes
        repo = _CoordinatedRepo()
        file_a = _make_legacy_file("file_conc_cancel")
        repo.files[file_a.id] = file_a
        attempt_ids = iter(("extract_attempt_a", "extract_attempt_b"))
        service = SessionFileService(
            repo,
            store,
            document_extractor=LocalSessionDocumentExtractor(),
            id_factory=lambda _: next(attempt_ids),
            clock=lambda: NOW,
        )

        # 启动双协程：拿到 attempt_a 者胜出提交，拿到 attempt_b 者在 put_bytes 阶段被取消。
        task_1 = asyncio.create_task(
            service.read_text("ses_1", "usr_1", file_a.id, offset=0, limit_bytes=4096)
        )
        task_2 = asyncio.create_task(
            service.read_text("ses_1", "usr_1", file_a.id, offset=0, limit_bytes=4096)
        )
        await asyncio.wait_for(a_committed.wait(), timeout=5.0)
        await asyncio.wait_for(b_entered_put.wait(), timeout=5.0)
        loser_task = loser_task_holder[0]
        winner_task = task_2 if loser_task is task_1 else task_1
        result_winner = await winner_task
        loser_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loser_task

        winner_key = "session-files-extracted/ab/extract_attempt_a.txt"
        loser_key = "session-files-extracted/ab/extract_attempt_b.txt"
        assert _ORPHAN_KEY.fullmatch(winner_key) is not None
        assert _ORPHAN_KEY.fullmatch(loser_key) is not None
        assert result_winner.file.extracted_text_key == winner_key
        assert store.deletes == [loser_key]
        assert winner_key in store.values
        assert loser_key not in store.values

        # 确认后续读取依然完全正常，不会因为另一协程取消导致缓存缺失。
        follow_up = await service.read_text("ses_1", "usr_1", file_a.id, offset=0, limit_bytes=4096)
        assert "## 第 1 页\nConcurrent Lazy Extraction Page 1" in follow_up.content

    asyncio.run(scenario_a_cancel())

    # ------------------------------------------------------------------
    # 场景 A2：协程 A 胜出提交 ready，协程 B 在 put_bytes 遇到存储异常
    # ------------------------------------------------------------------
    async def scenario_a_storage_error() -> None:
        a_committed = asyncio.Event()

        class _FailingSecondPutStore(_MemoryObjectStore):
            async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
                if key.endswith("/extract_err_b.txt"):
                    await a_committed.wait()
                    raise FileStorageError("file_storage_unavailable")
                await super().put_bytes(key, content, content_type=content_type)

        class _NotifyingRepo(_TwoReaderBarrierRepo):
            async def update_extraction(
                self,
                file_id: str,
                *,
                extraction_status: SessionFileExtractionStatus,
                extracted_text_key: str | None,
                extracted_text_bytes: int | None,
                extraction_error_code: str | None,
            ) -> SessionFile | None:
                result = await super().update_extraction(
                    file_id,
                    extraction_status=extraction_status,
                    extracted_text_key=extracted_text_key,
                    extracted_text_bytes=extracted_text_bytes,
                    extraction_error_code=extraction_error_code,
                )
                a_committed.set()
                return result

        store = _FailingSecondPutStore()
        store.values[orig_key] = pdf_bytes
        repo = _NotifyingRepo()
        file_err = _make_legacy_file("file_conc_err")
        repo.files[file_err.id] = file_err
        attempt_ids = iter(("extract_err_a", "extract_err_b"))
        service = SessionFileService(
            repo,
            store,
            document_extractor=LocalSessionDocumentExtractor(),
            id_factory=lambda _: next(attempt_ids),
            clock=lambda: NOW,
        )

        # 不依赖工作线程完成先后：并发收集两协程结果，验证一胜一败且胜者缓存未被误删。
        outcomes = await asyncio.gather(
            service.read_text("ses_1", "usr_1", file_err.id, offset=0, limit_bytes=4096),
            service.read_text("ses_1", "usr_1", file_err.id, offset=0, limit_bytes=4096),
            return_exceptions=True,
        )
        successes = [item for item in outcomes if not isinstance(item, BaseException)]
        failures = [item for item in outcomes if isinstance(item, BaseException)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], FileStorageError)

        winner_key = "session-files-extracted/ab/extract_err_a.txt"
        loser_key = "session-files-extracted/ab/extract_err_b.txt"
        assert successes[0].file.extracted_text_key == winner_key
        assert store.deletes == [loser_key]
        assert winner_key in store.values
        follow_up = await service.read_text(
            "ses_1",
            "usr_1",
            file_err.id,
            offset=0,
            limit_bytes=4096,
        )
        assert "## 第 1 页\nConcurrent Lazy Extraction Page 1" in follow_up.content

    asyncio.run(scenario_a_storage_error())

    # ------------------------------------------------------------------
    # 场景 B：双协程均写完各自独立尝试键后竞争 update_extraction，落败方仅删自身键
    # ------------------------------------------------------------------
    async def scenario_b_update_race() -> None:
        both_written = asyncio.Event()
        written_count = 0

        class _BarrierStore(_MemoryObjectStore):
            async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
                nonlocal written_count
                await super().put_bytes(key, content, content_type=content_type)
                written_count += 1
                if written_count == 2:
                    both_written.set()
                await asyncio.wait_for(both_written.wait(), timeout=5.0)

        store = _BarrierStore()
        store.values[orig_key] = pdf_bytes
        repo = _TwoReaderBarrierRepo()
        file_race = _make_legacy_file("file_conc_update_race")
        repo.files[file_race.id] = file_race
        attempt_ids = iter(("extract_race_1", "extract_race_2"))
        service = SessionFileService(
            repo,
            store,
            document_extractor=LocalSessionDocumentExtractor(),
            id_factory=lambda _: next(attempt_ids),
            clock=lambda: NOW,
        )

        slice_1, slice_2 = await asyncio.gather(
            service.read_text("ses_1", "usr_1", file_race.id, offset=0, limit_bytes=4096),
            service.read_text("ses_1", "usr_1", file_race.id, offset=0, limit_bytes=4096),
        )
        committed = repo.files[file_race.id]
        assert committed.extraction_status is SessionFileExtractionStatus.READY
        winner_key = committed.extracted_text_key
        assert winner_key in {
            "session-files-extracted/ab/extract_race_1.txt",
            "session-files-extracted/ab/extract_race_2.txt",
        }
        all_keys = {
            "session-files-extracted/ab/extract_race_1.txt",
            "session-files-extracted/ab/extract_race_2.txt",
        }
        loser_key = next(iter(all_keys - {winner_key}))
        assert store.deletes == [loser_key]
        assert winner_key in store.values
        assert loser_key not in store.values
        assert slice_1.content == slice_2.content
        assert "## 第 1 页\nConcurrent Lazy Extraction Page 1" in slice_1.content

        # 验证胜者缓存持续可读
        again = await service.read_text(
            "ses_1",
            "usr_1",
            file_race.id,
            offset=0,
            limit_bytes=4096,
        )
        assert again.content == slice_1.content

    asyncio.run(scenario_b_update_race())


def test_upload_cache_write_transient_failure_falls_back_to_none_and_self_heals(
    tmp_path: Path,
) -> None:
    """上传阶段缓存写入遇瞬态故障时回落为 none 不阻断原件上传，首次读取自动惰性补写恢复。"""
    pdf_path = tmp_path / "self_heal.pdf"
    pdf_path.write_bytes(_build_multipage_pdf(["Recoverable PDF Content"]))

    class _FlakyFirstCachePutStore(_MemoryObjectStore):
        def __init__(self) -> None:
            super().__init__()
            self.cache_put_attempts = 0

        async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
            if key.startswith("session-files-extracted/"):
                self.cache_put_attempts += 1
                if self.cache_put_attempts == 1:
                    raise FileStorageError("file_storage_unavailable")
            await super().put_bytes(key, content, content_type=content_type)

    store = _FlakyFirstCachePutStore()
    repo = _MemorySessionFileRepository()
    ids = iter(("file_heal_1", "extract_heal_1"))
    service = SessionFileService(
        repo,
        store,
        document_extractor=LocalSessionDocumentExtractor(),
        id_factory=lambda _: next(ids),
        clock=lambda: NOW,
    )

    async def scenario() -> None:
        uploaded = await service.upload(
            "ses_1",
            "usr_1",
            idempotency_key="k-self-heal",
            filename="self_heal.pdf",
            declared_mime="application/pdf",
            path=pdf_path,
        )
        # 原件正常落库，提取状态回落为 none，且清理了失败的上传期缓存对象键。
        assert uploaded.object_key in store.values
        assert uploaded.extraction_status is SessionFileExtractionStatus.NONE
        assert uploaded.extracted_text_key is None
        assert uploaded.extracted_text_bytes is None
        assert uploaded.extraction_error_code is None
        assert f"session-files-extracted/{uploaded.sha256[:2]}/file_heal_1.txt" in store.deletes

        # 首次 read_text 通过惰性补解析自动恢复为 ready 并返回正文。
        recovered = await service.read_text(
            "ses_1",
            "usr_1",
            uploaded.id,
            offset=0,
            limit_bytes=4096,
        )
        assert recovered.source_kind == "extracted_document"
        assert recovered.file.extraction_status is SessionFileExtractionStatus.READY
        assert recovered.file.extracted_text_key == (
            f"session-files-extracted/{uploaded.sha256[:2]}/extract_heal_1.txt"
        )
        assert "## 第 1 页\nRecoverable PDF Content" in recovered.content

    asyncio.run(scenario())


def test_publish_transaction_failure_cleans_both_original_and_extracted_cache_orphans(
    tmp_path: Path,
) -> None:
    """发布事务确定性拒绝（如配额超限/会话不存在）或并发落败时，必须同时清理原件与提取缓存孤儿。"""
    pdf_path = tmp_path / "orphan.pdf"
    pdf_path.write_bytes(_build_multipage_pdf(["Orphan cleanup verification"]))

    # 场景 A：配额超限拒绝
    objects = _MemoryObjectStore()

    class _QuotaExceededRepo(_MemorySessionFileRepository):
        async def publish_upload(self, *args: Any, **kwargs: Any) -> SessionFile:
            del args, kwargs
            raise ValueError("session_file_quota_exceeded")

    service_quota = SessionFileService(
        _QuotaExceededRepo(),
        objects,
        document_extractor=LocalSessionDocumentExtractor(),
        id_factory=lambda _: "file_quota_fail",
        clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="session_file_quota_exceeded"):
        asyncio.run(
            service_quota.upload(
                "ses_1",
                "usr_1",
                idempotency_key="k-quota",
                filename="orphan.pdf",
                declared_mime="application/pdf",
                path=pdf_path,
            )
        )
    assert len(objects.puts) == 2
    assert objects.values == {}
    assert any(k.startswith("session-files/") for k in objects.deletes)
    assert any(k.startswith("session-files-extracted/") for k in objects.deletes)

    # 场景 B：并发同键胜者已提交另一 file_id，败者同时清理自身原件与提取缓存
    objects_race = _MemoryObjectStore()

    class _ConcurrentWinnerRepo(_MemorySessionFileRepository):
        async def publish_upload(
            self,
            session_id: str,
            owner_id: str,
            file: PendingSessionFile,
            *,
            idempotency_key: str,
            request_fingerprint: str,
            published_at: datetime,
        ) -> SessionFile:
            del idempotency_key, request_fingerprint, published_at
            return SessionFile(
                id="file_winner",
                session_id=session_id,
                owner_id=owner_id,
                source=SessionFileSource.UPLOAD,
                filename=file.filename,
                label=None,
                mime_type=file.mime_type,
                size_bytes=file.size_bytes,
                sha256=file.sha256,
                object_key=f"session-files/{file.sha256[:2]}/file_winner",
                source_call_id=None,
                output_index=None,
                created_at=file.created_at,
                extraction_status=SessionFileExtractionStatus.READY,
                extracted_text_key=f"session-files-extracted/{file.sha256[:2]}/file_winner.txt",
                extracted_text_bytes=file.extracted_text_bytes,
                extraction_error_code=None,
            )

    service_race = SessionFileService(
        _ConcurrentWinnerRepo(),
        objects_race,
        document_extractor=LocalSessionDocumentExtractor(),
        id_factory=lambda _: "file_loser",
        clock=lambda: NOW,
    )
    winner = asyncio.run(
        service_race.upload(
            "ses_1",
            "usr_1",
            idempotency_key="k-race",
            filename="orphan.pdf",
            declared_mime="application/pdf",
            path=pdf_path,
        )
    )
    assert winner.id == "file_winner"
    assert objects_race.values == {}
    assert any(k.endswith("/file_loser") for k in objects_race.deletes)
    assert any(k.endswith("/file_loser.txt") for k in objects_race.deletes)

    # 场景 C：会话不存在（SessionNotFoundError）与幂等冲突（idempotency_conflict）同样双删孤儿
    for exc_factory, expected_exc in (
        (lambda: SessionNotFoundError("session_not_found"), SessionNotFoundError),
        (lambda: ValueError("idempotency_conflict"), FileInputError),
    ):
        objs = _MemoryObjectStore()

        class _RejectRepo(_MemorySessionFileRepository):
            def __init__(self, factory: Any) -> None:
                super().__init__()
                self._factory = factory

            async def publish_upload(self, *args: Any, **kwargs: Any) -> SessionFile:
                del args, kwargs
                raise self._factory()

        svc = SessionFileService(
            _RejectRepo(exc_factory),
            objs,
            document_extractor=LocalSessionDocumentExtractor(),
            id_factory=lambda _: "file_rejected",
            clock=lambda: NOW,
        )
        with pytest.raises(expected_exc):
            asyncio.run(
                svc.upload(
                    "ses_missing",
                    "usr_1",
                    idempotency_key="k-reject",
                    filename="orphan.pdf",
                    declared_mime="application/pdf",
                    path=pdf_path,
                )
            )
        assert len(objs.puts) == 2
        assert objs.values == {}
        assert any(k.endswith("/file_rejected") for k in objs.deletes)
        assert any(k.endswith("/file_rejected.txt") for k in objs.deletes)


# ---------------------------------------------------------------------------
# 3. SQLAlchemy 仓储、引用保护与离线对象维护集成测试
# ---------------------------------------------------------------------------


def test_sqlalchemy_repository_extraction_and_object_reference_protection() -> None:
    """验证 SQLAlchemy 仓储的提取字段持久化、并发保护及 is_object_key_referenced 缓存保护。"""

    async def scenario() -> None:
        async with pg_unit_db() as (_engine, factory):
            # 预置用户与会话（PG 外键约束下先 flush UserModel 再写入 SessionModel）
            async with factory() as session:
                async with session.begin():
                    session.add(
                        UserModel(
                            id="usr_1",
                            email="u1@example.com",
                            password_hash="hash",
                            status=UserStatus.ACTIVE.value,
                            role=UserRole.MEMBER.value,
                            created_at=NOW,
                            updated_at=NOW,
                        )
                    )
                    await session.flush()
                    session.add(
                        SessionModel(
                            id="ses_1",
                            owner_id="usr_1",
                            title="测试会话",
                            created_at=NOW,
                            updated_at=NOW,
                        )
                    )

            repo = SqlAlchemySessionFileRepository(factory)
            orig_key = "session-files/ab/file_db_1"
            cache_key = "session-files-extracted/ab/file_db_1.txt"

            # 写前组合一致性对称校验：publish_upload 与 pending_file_to_model 快速拒绝非法组合
            invalid_pending = PendingSessionFile(
                id="file_invalid_fields",
                filename="bad.pdf",
                label=None,
                mime_type="application/pdf",
                size_bytes=10,
                sha256="ab" * 32,
                object_key="session-files/ab/file_invalid_fields",
                output_index=-1,
                created_at=NOW,
                extraction_status=SessionFileExtractionStatus.READY,
                extracted_text_key=None,
                extracted_text_bytes=None,
                extraction_error_code=None,
            )
            with pytest.raises(ValueError, match="session_file_extraction_fields_invalid"):
                pending_file_to_model(
                    invalid_pending,
                    session_id="ses_1",
                    owner_id="usr_1",
                    source="upload",
                    source_call_id=None,
                )
            with pytest.raises(ValueError, match="session_file_extraction_fields_invalid"):
                await repo.publish_upload(
                    "ses_1",
                    "usr_1",
                    invalid_pending,
                    idempotency_key="idem-invalid",
                    request_fingerprint="fp-invalid",
                    published_at=NOW,
                )

            pending = PendingSessionFile(
                id="file_db_1",
                filename="spec.pdf",
                label=None,
                mime_type="application/pdf",
                size_bytes=512,
                sha256="ab" * 32,
                object_key=orig_key,
                output_index=-1,
                created_at=NOW,
                extraction_status=SessionFileExtractionStatus.NONE,
            )
            published = await repo.publish_upload(
                "ses_1",
                "usr_1",
                pending,
                idempotency_key="idem-db-1",
                request_fingerprint="fp-db-1",
                published_at=NOW,
            )
            assert published.extraction_status is SessionFileExtractionStatus.NONE
            assert await repo.is_object_key_referenced(orig_key) is True
            assert await repo.is_object_key_referenced(cache_key) is False

            # 回填 ready 状态
            updated = await repo.update_extraction(
                "file_db_1",
                extraction_status=SessionFileExtractionStatus.READY,
                extracted_text_key=cache_key,
                extracted_text_bytes=128,
                extraction_error_code=None,
            )
            assert updated is not None
            assert updated.extraction_status is SessionFileExtractionStatus.READY
            assert updated.extracted_text_key == cache_key
            assert updated.extracted_text_bytes == 128

            # 并发再次尝试覆盖已 ready 记录 -> 保留原 ready 键不被覆盖
            second_update = await repo.update_extraction(
                "file_db_1",
                extraction_status=SessionFileExtractionStatus.READY,
                extracted_text_key="session-files-extracted/ab/other.txt",
                extracted_text_bytes=999,
                extraction_error_code=None,
            )
            assert second_update is not None
            assert second_update.extracted_text_key == cache_key
            assert second_update.extracted_text_bytes == 128

            # 引用复核必须同时保护原件与提取缓存，未引用孤儿返回 False
            assert await repo.is_object_key_referenced(orig_key) is True
            assert await repo.is_object_key_referenced(cache_key) is True
            orphan_cache_key = "session-files-extracted/cd/file_orphan.txt"
            assert await repo.is_object_key_referenced(orphan_cache_key) is False

            # 验证 MinioFileObjectStore 放行 session-files-extracted/ 并纳入离线维护扫描与清理
            @dataclass(frozen=True, slots=True)
            class _Listed:
                object_name: str
                last_modified: datetime

            class _FakeMinio:
                def __init__(self) -> None:
                    self.removed: list[str] = []

                def list_objects(
                    self,
                    bucket_name: str,
                    *,
                    recursive: bool,
                    start_after: str | None = None,
                ) -> object:
                    del bucket_name, recursive
                    items = (
                        _Listed(orig_key, NOW.replace(day=1)),
                        _Listed(cache_key, NOW.replace(day=1)),
                        _Listed(orphan_cache_key, NOW.replace(day=1)),
                    )
                    return iter(
                        item
                        for item in items
                        if start_after is None or item.object_name > start_after
                    )

                def remove_object(self, bucket_name: str, object_name: str) -> None:
                    del bucket_name
                    self.removed.append(object_name)

                def put_object(
                    self,
                    bucket_name: str,
                    object_name: str,
                    data: Any,
                    length: int,
                    *,
                    content_type: str,
                    num_parallel_uploads: int,
                ) -> None:
                    del bucket_name, object_name, data, length, content_type, num_parallel_uploads

            fake_minio = _FakeMinio()
            minio_store = MinioFileObjectStore(
                MinioFileStorageConfig(
                    endpoint="minio.example.test:443",
                    access_key="ak",
                    secret_key="sk",
                    bucket="athena-files-test",
                    secure=True,
                ),
                client=fake_minio,  # type: ignore[arg-type]
                clock=lambda: NOW,
            )
            try:
                # 确认对象存储键校验显式允许 session-files-extracted/ 前缀。
                await minio_store.put_bytes(
                    cache_key,
                    b"ok",
                    content_type="text/plain; charset=utf-8",
                )
                maintenance = FileObjectMaintenance(store=minio_store, references=repo)
                result = await maintenance.run(execute=True, writers_stopped=True)
                assert result.candidate_count == 3
                assert result.referenced_count == 2
                assert result.deleted_count == 1
                assert fake_minio.removed == [orphan_cache_key]
            finally:
                await minio_store.aclose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 4. HTTP 安全预览 (/preview) 与分页正文 (/content) API 集成测试
# ---------------------------------------------------------------------------


def test_http_api_content_and_preview_endpoints_security_and_pagination() -> None:
    """验证 /preview 严格位图白名单与安全响应头、/content 分页/错误码及跨用户 404 隔离。"""
    objects = _MemoryObjectStore()
    repository = _MemorySessionFileRepository()
    counter = 0

    def next_id(_: str) -> str:
        nonlocal counter
        counter += 1
        return f"file_api_{counter}"

    service = SessionFileService(
        repository,
        objects,
        document_extractor=LocalSessionDocumentExtractor(),
        id_factory=next_id,
        clock=lambda: NOW,
    )
    auth_service = make_auth_service()
    app = create_app(auth_service=auth_service, session_file_service=service)

    auth_client = AuthTestClient(app)
    with auth_client as client:
        # 1. 上传多种文件：TXT、PDF、DOCX、空白 PDF、PNG、JPEG、WEBP
        r_txt = client.post(
            "/api/v1/sessions/ses_1/files",
            headers={"Idempotency-Key": "up-txt"},
            files={"file": ("notes.txt", "第一行\n第二行".encode(), "text/plain")},
        )
        assert r_txt.status_code == 201
        txt_data = r_txt.json()
        assert txt_data["extraction_status"] == "none"
        assert txt_data["extracted_text_bytes"] is None
        assert txt_data["extraction_error_code"] is None
        assert "object_key" not in txt_data
        assert "extracted_text_key" not in txt_data

        r_pdf = client.post(
            "/api/v1/sessions/ses_1/files",
            headers={"Idempotency-Key": "up-pdf"},
            files={
                "file": (
                    "contract.pdf",
                    _build_multipage_pdf(["Clause One", "Clause Two"]),
                    "application/pdf",
                )
            },
        )
        assert r_pdf.status_code == 201
        pdf_data = r_pdf.json()
        assert pdf_data["extraction_status"] == "ready"
        assert pdf_data["extracted_text_bytes"] > 0
        assert pdf_data["extraction_error_code"] is None

        r_docx = client.post(
            "/api/v1/sessions/ses_1/files",
            headers={"Idempotency-Key": "up-docx"},
            files={
                "file": (
                    "summary.docx",
                    _build_docx_bytes(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert r_docx.status_code == 201
        assert r_docx.json()["extraction_status"] == "ready"

        r_blank = client.post(
            "/api/v1/sessions/ses_1/files",
            headers={"Idempotency-Key": "up-blank"},
            files={"file": ("scan.pdf", _build_blank_pdf(), "application/pdf")},
        )
        assert r_blank.status_code == 201
        blank_data = r_blank.json()
        assert blank_data["extraction_status"] == "failed"
        assert blank_data["extraction_error_code"] == "document_no_extractable_text"

        r_png = client.post(
            "/api/v1/sessions/ses_1/files",
            headers={"Idempotency-Key": "up-png"},
            files={"file": ("图表.png", _build_image_bytes("PNG"), "image/png")},
        )
        assert r_png.status_code == 201
        png_id = r_png.json()["id"]

        r_jpg = client.post(
            "/api/v1/sessions/ses_1/files",
            headers={"Idempotency-Key": "up-jpg"},
            files={"file": ("photo.jpg", _build_image_bytes("JPEG"), "image/jpeg")},
        )
        assert r_jpg.status_code == 201
        jpg_id = r_jpg.json()["id"]

        r_webp = client.post(
            "/api/v1/sessions/ses_1/files",
            headers={"Idempotency-Key": "up-webp"},
            files={"file": ("banner.webp", _build_image_bytes("WEBP"), "image/webp")},
        )
        assert r_webp.status_code == 201
        webp_id = r_webp.json()["id"]

        # 2. 验证 /preview 仅放行 PNG/JPEG/WEBP，并强制 inline + nosniff + sandbox CSP + no-store
        for image_id, expected_mime in (
            (png_id, "image/png"),
            (jpg_id, "image/jpeg"),
            (webp_id, "image/webp"),
        ):
            preview = client.get(f"/api/v1/sessions/ses_1/files/{image_id}/preview")
            assert preview.status_code == 200
            assert preview.headers["content-type"].startswith(expected_mime)
            assert preview.headers["content-disposition"].startswith("inline;")
            assert preview.headers["x-content-type-options"] == "nosniff"
            assert preview.headers["content-security-policy"] == "default-src 'none'; sandbox"
            assert preview.headers["cache-control"] == "private, no-store"
            assert int(preview.headers["content-length"]) == len(preview.content)

        # 3. 验证 /preview 对 PDF、TXT、DOCX、HTML/SVG 一律拒绝并返回 415 file_preview_unsupported
        for non_preview_id in (pdf_data["id"], txt_data["id"], r_docx.json()["id"]):
            denied = client.get(f"/api/v1/sessions/ses_1/files/{non_preview_id}/preview")
            assert denied.status_code == 415
            assert denied.json() == {"detail": "file_preview_unsupported"}

        # 额外构造危险类型（SVG / HTML）直接写入仓储，验证即使历史元数据存在也严格 415 拒绝
        owner_id = auth_client.user_id
        assert owner_id is not None
        for idx, (fname, mime) in enumerate(
            (("xss.svg", "image/svg+xml"), ("page.html", "text/html")),
            1,
        ):
            danger_id = f"file_danger_{idx}"
            repository.files[danger_id] = SessionFile(
                id=danger_id,
                session_id="ses_1",
                owner_id=owner_id,
                source=SessionFileSource.UPLOAD,
                filename=fname,
                label=None,
                mime_type=mime,
                size_bytes=16,
                sha256="aa" * 32,
                object_key=f"session-files/aa/{danger_id}",
                source_call_id=None,
                output_index=None,
                created_at=NOW,
            )
            objects.values[f"session-files/aa/{danger_id}"] = b"<svg onload=alert(1)/>"
            resp_danger = client.get(f"/api/v1/sessions/ses_1/files/{danger_id}/preview")
            assert resp_danger.status_code == 415
            assert resp_danger.json() == {"detail": "file_preview_unsupported"}

        # 4. 验证 /content 支持纯文本 (raw_text) 与 PDF/DOCX (extracted_document) 分页
        c_txt = client.get(f"/api/v1/sessions/ses_1/files/{txt_data['id']}/content")
        assert c_txt.status_code == 200
        assert c_txt.headers["cache-control"] == "private, no-store"
        body_txt = c_txt.json()
        assert body_txt["source_kind"] == "raw_text"
        assert body_txt["content"] == "第一行\n第二行"
        assert body_txt["eof"] is True
        assert body_txt["truncated"] is False

        c_pdf_1 = client.get(
            f"/api/v1/sessions/ses_1/files/{pdf_data['id']}/content",
            params={"offset": 0, "limit_bytes": 20},
        )
        assert c_pdf_1.status_code == 200
        assert c_pdf_1.headers["cache-control"] == "private, no-store"
        p1 = c_pdf_1.json()
        assert p1["source_kind"] == "extracted_document"
        assert p1["truncated"] is True
        assert p1["eof"] is False

        c_pdf_2 = client.get(
            f"/api/v1/sessions/ses_1/files/{pdf_data['id']}/content",
            params={"offset": p1["next_offset"], "limit_bytes": 65536},
        )
        assert c_pdf_2.status_code == 200
        p2 = c_pdf_2.json()
        assert p2["eof"] is True
        combined_pdf = p1["content"] + p2["content"]
        assert "## 第 1 页\nClause One" in combined_pdf
        assert "## 第 2 页\nClause Two" in combined_pdf

        # 5. 验证 /content 对图片返回 415 file_not_text、提取失败返回 422 稳定码、非法范围返回 422
        c_img = client.get(f"/api/v1/sessions/ses_1/files/{png_id}/content")
        assert c_img.status_code == 415
        assert c_img.json() == {"detail": "file_not_text"}

        c_blank = client.get(f"/api/v1/sessions/ses_1/files/{blank_data['id']}/content")
        assert c_blank.status_code == 422
        assert c_blank.json() == {"detail": "document_no_extractable_text"}

        c_bad_offset = client.get(
            f"/api/v1/sessions/ses_1/files/{pdf_data['id']}/content",
            params={"offset": 999999},
        )
        assert c_bad_offset.status_code == 422
        assert c_bad_offset.json() == {"detail": "file_offset_invalid"}

        c_bad_query = client.get(
            f"/api/v1/sessions/ses_1/files/{pdf_data['id']}/content",
            params={"offset": -1},
        )
        assert c_bad_query.status_code == 422

        # 6. 验证跨会话/跨用户越权请求均返回 404 session_file_not_found
        other_user_file = replace(
            repository.files[png_id],
            id="file_other_owner",
            owner_id="usr_someone_else",
        )
        repository.files[other_user_file.id] = other_user_file
        assert (
            client.get("/api/v1/sessions/ses_1/files/file_other_owner/preview").status_code == 404
        )
        assert (
            client.get("/api/v1/sessions/ses_1/files/file_other_owner/content").status_code == 404
        )
        assert client.get(f"/api/v1/sessions/ses_other/files/{png_id}/preview").status_code == 404
        assert (
            client.get(f"/api/v1/sessions/ses_other/files/{pdf_data['id']}/content").status_code
            == 404
        )


def test_preview_prefetch_maps_storage_error_to_503_and_closes_stream() -> None:
    """预览流首块预读遇到对象存储异常时必须在发送 200 前返回 503 并关闭底层流。"""

    class _FailingPreviewService:
        def __init__(self) -> None:
            self.closed = False

        async def get(self, *_: object) -> SessionFile:
            return SessionFile(
                id="file_png",
                session_id="ses_1",
                owner_id="usr_1",
                source=SessionFileSource.UPLOAD,
                filename="chart.png",
                label=None,
                mime_type="image/png",
                size_bytes=10,
                sha256="aa" * 32,
                object_key="session-files/aa/file_png",
                source_call_id=None,
                output_index=None,
                created_at=NOW,
            )

        async def iter_download(self, _: object) -> AsyncIterator[bytes]:
            try:
                raise FileStorageError("file_storage_not_found")
                yield b""  # pragma: no cover
            finally:
                self.closed = True

    service = _FailingPreviewService()
    app = create_app(
        auth_service=make_auth_service(),
        session_file_service=cast(SessionFileService, service),
    )
    auth_client = AuthTestClient(app)
    with auth_client as client:
        resp = client.get("/api/v1/sessions/ses_1/files/file_png/preview")

    assert resp.status_code == 503
    assert resp.json() == {"detail": "file_storage_unavailable"}
    assert service.closed is True


# ---------------------------------------------------------------------------
# 5. 工具运行时、Run 上下文、SafeStop、Bootstrap 与 Alembic 迁移测试
# ---------------------------------------------------------------------------


def test_session_file_read_tool_reads_office_and_pdf_and_formats_errors(tmp_path: Path) -> None:
    """session_file_read 工具可读取 PDF/DOCX/XLSX/PPTX 并返回 source_kind 及清晰中文错误提示。"""
    objects = _MemoryObjectStore()
    repository = _MemorySessionFileRepository()
    ids = iter(("file_t_pdf", "file_t_xlsx", "file_t_blank", "file_t_png"))
    file_service = SessionFileService(
        repository,
        objects,
        document_extractor=LocalSessionDocumentExtractor(),
        id_factory=lambda _: next(ids),
        clock=lambda: NOW,
    )

    class _AuthReader:
        async def get_current_session_user(
            self, owner_id: str, auth_session_id: str, now: datetime
        ) -> User | None:
            del auth_session_id, now
            return User(
                id=owner_id,
                email="u@example.com",
                password_hash="hash",
                display_name="tester",
                status=UserStatus.ACTIVE,
                role=UserRole.MEMBER,
                created_at=NOW,
                updated_at=NOW,
            )

    class _Sessions:
        async def get_visible(self, scope: SessionScope) -> Session | None:
            return Session(
                id=scope.session_id,
                owner_id=scope.owner_id,
                title="s",
                project_id=None,
                last_message_sequence=0,
                created_at=NOW,
                updated_at=NOW,
                deleted_time=None,
            )

    class _EmptyMcpCatalog:
        async def list_runtime_tools(self, *, limit: int = 201) -> tuple[Any, ...]:
            del limit
            return ()

        async def get_call_target(self, *_: Any) -> None:
            return None

    class _NoopMcpExecutor:
        async def call(self, *_: Any, **__: Any) -> Any:
            raise AssertionError("not used")

    runtime = RunToolRuntimeService(
        auth=cast(Any, _AuthReader()),
        sessions=cast(Any, _Sessions()),
        mcp_catalog=cast(Any, _EmptyMcpCatalog()),
        mcp_executor=cast(Any, _NoopMcpExecutor()),
        knowledge=None,
        project_knowledge=None,
        session_files=file_service,
        clock=lambda: NOW,
    )

    assert "PDF" in SESSION_FILE_READ_DEFINITION.description
    assert "Word" in SESSION_FILE_READ_DEFINITION.description
    assert "Excel" in SESSION_FILE_READ_DEFINITION.description
    assert "PPT" in SESSION_FILE_READ_DEFINITION.description

    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(_build_multipage_pdf(["Tool Read PDF Content"]))
    xlsx_path = tmp_path / "sheet.xlsx"
    xlsx_path.write_bytes(_build_xlsx_bytes())
    blank_path = tmp_path / "empty.pdf"
    blank_path.write_bytes(_build_blank_pdf())
    png_path = tmp_path / "pic.png"
    png_path.write_bytes(_build_image_bytes("PNG"))

    ctx = ToolExecutionContext(
        run_id="run_1",
        owner_id="usr_1",
        chat_session_id="ses_1",
        project_id=None,
        execution_epoch=1,
        auth_session_id="auth_1",
        deadline_at=NOW,
    )

    async def scenario() -> None:
        f_pdf = await file_service.upload(
            "ses_1",
            "usr_1",
            idempotency_key="t-pdf",
            filename="doc.pdf",
            declared_mime="application/pdf",
            path=pdf_path,
        )
        f_xlsx = await file_service.upload(
            "ses_1",
            "usr_1",
            idempotency_key="t-xlsx",
            filename="sheet.xlsx",
            declared_mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            path=xlsx_path,
        )
        f_blank = await file_service.upload(
            "ses_1",
            "usr_1",
            idempotency_key="t-blank",
            filename="empty.pdf",
            declared_mime="application/pdf",
            path=blank_path,
        )
        f_png = await file_service.upload(
            "ses_1",
            "usr_1",
            idempotency_key="t-png",
            filename="pic.png",
            declared_mime="image/png",
            path=png_path,
        )

        # 读取 PDF
        res_pdf = await runtime.execute(
            SESSION_FILE_READ_DEFINITION,
            "session_file_read",
            {"file_id": f_pdf.id, "offset": 0, "limit_bytes": 4096},
            ctx,
            deadline_seconds=10.0,
            run_remaining_seconds=10.0,
        )
        assert res_pdf.outcome.status is ToolOutcomeStatus.OK
        assert res_pdf.outcome.data["source_kind"] == "extracted_document"
        assert "## 第 1 页\nTool Read PDF Content" in str(res_pdf.outcome.data["content"])
        file_meta = cast(dict[str, Any], res_pdf.outcome.data["file"])
        assert file_meta["extraction_status"] == "ready"
        assert file_meta["extracted_text_bytes"] == f_pdf.extracted_text_bytes

        # 读取 XLSX
        res_xlsx = await runtime.execute(
            SESSION_FILE_READ_DEFINITION,
            "session_file_read",
            {"file_id": f_xlsx.id},
            ctx,
            deadline_seconds=10.0,
            run_remaining_seconds=10.0,
        )
        assert res_xlsx.outcome.status is ToolOutcomeStatus.OK
        assert res_xlsx.outcome.data["source_kind"] == "extracted_document"
        assert "## 工作表：销售明细" in str(res_xlsx.outcome.data["content"])

        # 读取无文本层 PDF -> 稳定错误码 + 中文业务提示
        res_blank = await runtime.execute(
            SESSION_FILE_READ_DEFINITION,
            "session_file_read",
            {"file_id": f_blank.id},
            ctx,
            deadline_seconds=10.0,
            run_remaining_seconds=10.0,
        )
        assert res_blank.outcome.status is ToolOutcomeStatus.ERROR
        assert res_blank.outcome.code == "document_no_extractable_text"
        assert "未检测到可提取的文本层" in res_blank.outcome.message

        # 读取加密文档 -> document_encrypted + 中文业务提示
        encrypted_file = replace(
            f_blank,
            id="file_t_encrypted",
            filename="secret.pdf",
            extraction_status=SessionFileExtractionStatus.FAILED,
            extraction_error_code="document_encrypted",
        )
        repository.files[encrypted_file.id] = encrypted_file
        res_enc = await runtime.execute(
            SESSION_FILE_READ_DEFINITION,
            "session_file_read",
            {"file_id": encrypted_file.id},
            ctx,
            deadline_seconds=10.0,
            run_remaining_seconds=10.0,
        )
        assert res_enc.outcome.status is ToolOutcomeStatus.ERROR
        assert res_enc.outcome.code == "document_encrypted"
        assert "加密保护" in res_enc.outcome.message

        # 读取图片 -> file_not_text
        res_png = await runtime.execute(
            SESSION_FILE_READ_DEFINITION,
            "session_file_read",
            {"file_id": f_png.id},
            ctx,
            deadline_seconds=10.0,
            run_remaining_seconds=10.0,
        )
        assert res_png.outcome.status is ToolOutcomeStatus.ERROR
        assert res_png.outcome.code == "file_not_text"

        # 验证 build_run_input_file_context、context.py 与 SafeStop 往返保真
        dummy_run = Run(
            id="run_1",
            session_id="ses_1",
            owner_id="usr_1",
            status=RunStatus.RUNNING,
            trigger_message_id="msg_1",
            context_cutoff_sequence=1,
            retry_of_run_id=None,
            model_config_version_id="mpv_1",
            main_prompt_version_id="spv_1",
            compression_prompt_version_id="spv_2",
            prompt_template_version="run-v2",
            event_sequence=1,
            execution_epoch=1,
            worker_id="worker_1",
            lease_expires_at=NOW,
            execution_started_at=NOW,
            execution_deadline_at=NOW,
            queued_at=NOW,
            first_started_at=NOW,
            finished_at=None,
            created_at=NOW,
            updated_at=NOW,
            cancel_requested_at=None,
            safe_error_code=None,
            usage=RunUsage(),
            input_files=RunInputFiles(file_ids=(f_pdf.id, f_blank.id)),
        )
        input_contexts = await runtime.build_run_input_file_context(dummy_run)
        assert len(input_contexts) == 2
        assert input_contexts[0].extraction_status == "ready"
        assert input_contexts[0].extracted_text_bytes == f_pdf.extracted_text_bytes
        assert input_contexts[0].extraction_error_code is None
        assert input_contexts[1].extraction_status == "failed"
        assert input_contexts[1].extraction_error_code == "document_no_extractable_text"

        # 兼容旧快照默认构造
        legacy_ctx = RunInputFileContext(
            file_id="f_old",
            filename="old.txt",
            label=None,
            mime_type="text/plain",
            size_bytes=5,
            source="upload",
        )
        assert legacy_ctx.extraction_status == "none"
        assert legacy_ctx.extracted_text_bytes is None
        assert legacy_ctx.extraction_error_code is None

        # 验证 messages_to_llm 系统提示与不可信文件元数据 JSON
        llm_messages = messages_to_llm(
            dummy_run,
            (),
            main_prompt="sys",
            input_files=input_contexts,
        )
        system_file_msg = next(
            m.content for m in llm_messages if "session_file_read" in (m.content or "")
        )
        assert "PDF" in system_file_msg and "Word" in system_file_msg
        user_file_msg = next(
            m.content
            for m in llm_messages
            if "<untrusted_input_file_metadata>" in (m.content or "")
        )
        json_segment = user_file_msg.split("<untrusted_input_file_metadata>\n", 1)[1].split(
            "\n</untrusted_input_file_metadata>", 1
        )[0]
        parsed_meta = json.loads(json_segment)
        assert parsed_meta[0]["extraction_status"] == "ready"
        assert parsed_meta[0]["extracted_text_bytes"] == f_pdf.extracted_text_bytes
        assert parsed_meta[1]["extraction_error_code"] == "document_no_extractable_text"

        # 验证 SafeStopReporter 安全停机报告只产出简洁系统提示，绝不回显提取正文或敏感载荷
        captured_commands: list[Any] = []

        class _CapturingRunRepo:
            async def is_live_execution(self, *_: Any, **__: Any) -> bool:
                return True

            async def fail(self, command: Any) -> Any:
                captured_commands.append(command)
                return dummy_run

        reporter = SafeStopReporter(
            cast(Any, _CapturingRunRepo()),
            id_factory=lambda p: f"{p}_1",
            now=lambda: NOW,
        )
        await reporter.stop_for_context_safety(
            dummy_run,
            (),
            ContextStopReason.HARD_WINDOW_EXCEEDED,
            authorize=None,
            publish_events=AsyncMock(),
        )
        assert len(captured_commands) == 1
        assert captured_commands[0].safe_error_code == "run_context_safety_stop"
        assert captured_commands[0].report_message is not None
        assert captured_commands[0].report_message.metadata.provider == "system_context_safety"
        assert "任务未标记为完成" in captured_commands[0].report_message.parts[0].text

    asyncio.run(scenario())


def test_bootstrap_injects_local_session_document_extractor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 API 与 Run Worker 两个组合根均为 SessionFileService 注入真实提取器。"""
    monkeypatch.setenv(
        "ATHENA_DATABASE_URL",
        "postgresql+asyncpg://user:password@127.0.0.1:5432/athena_test",
    )
    monkeypatch.setenv("ATHENA_STORAGE_ENDPOINT", "minio.example.test:9000")
    monkeypatch.setenv("ATHENA_STORAGE_ACCESS_KEY", "test-ak")
    monkeypatch.setenv("ATHENA_STORAGE_SECRET_KEY", "test-sk")
    monkeypatch.setenv("ATHENA_STORAGE_BUCKET", "athena-test")
    monkeypatch.setenv("ATHENA_STORAGE_SECURE", "false")
    monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv(
        "ATHENA_MCP_SECRET_KEY",
        "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
    )
    monkeypatch.setenv("ATHENA_ENV", "development")

    api_deps = build_api_dependencies()
    try:
        assert api_deps.session_file_service is not None
        assert isinstance(
            api_deps.session_file_service._document_extractor,
            LocalSessionDocumentExtractor,
        )
    finally:
        asyncio.run(api_deps.aclose())

    bundle = RunWorkerBundle()
    try:
        build_run_worker_dependencies(bundle)
        assert bundle.worker is not None
        tool_runtime = bundle.worker._executor._tool_runtime
        assert isinstance(tool_runtime, RunToolRuntimeService)
        assert tool_runtime._session_files is not None
        assert isinstance(
            tool_runtime._session_files._document_extractor,
            LocalSessionDocumentExtractor,
        )
    finally:

        async def _cleanup() -> None:
            if bundle.mcp_executor is not None:
                await bundle.mcp_executor.aclose()
            if bundle.skill_package_cache is not None:
                await bundle.skill_package_cache.aclose()
            if bundle.file_object_store is not None:
                await bundle.file_object_store.aclose()
            if bundle.provider is not None:
                await bundle.provider.aclose()
            if bundle.query_services is not None:
                await bundle.query_services.aclose()
            if bundle.storage_client is not None:
                await bundle.storage_client.aclose()
            if bundle.database is not None:
                await bundle.database.dispose()

        asyncio.run(_cleanup())


def test_alembic_migration_0038_single_head_pg_sql_and_sqlite_roundtrip(tmp_path: Path) -> None:
    """验证 20261205_0038 单一 head、PostgreSQL 离线 DDL 与 SQLite 在线升级/约束/降级。"""
    pg_cfg = Config(str(ALEMBIC_INI))
    pg_cfg.set_main_option(
        "sqlalchemy.url",
        "postgresql+asyncpg://user:password@127.0.0.1/athena_test",
    )
    script = ScriptDirectory.from_config(pg_cfg)
    # 保持 Alembic 单一 head，并验证 20261205_0038 位于主干迁移链中（兼容后续 M3.3+ 迁移）。
    assert len(script.get_heads()) == 1
    rev = script.get_revision("20261205_0038")
    assert rev is not None
    assert rev.down_revision == "20261204_0037"

    # 1. PostgreSQL 离线升级与回滚 SQL
    up_buf = StringIO()
    with redirect_stdout(up_buf):
        command.upgrade(pg_cfg, "20261204_0037:20261205_0038", sql=True)
    up_sql = up_buf.getvalue()
    assert "ADD COLUMN extraction_status VARCHAR(16) DEFAULT 'none' NOT NULL" in up_sql
    assert "ADD COLUMN extracted_text_key VARCHAR(1024)" in up_sql
    assert "ADD COLUMN extracted_text_bytes BIGINT" in up_sql
    assert "ADD COLUMN extraction_error_code VARCHAR(64)" in up_sql
    assert "ck_session_files_extraction_status" in up_sql
    assert "ck_session_files_extracted_text_bytes" in up_sql
    assert "ck_session_files_extraction_consistency" in up_sql
    assert "CREATE UNIQUE INDEX uq_session_files_extracted_text_key" in up_sql

    down_buf = StringIO()
    with redirect_stdout(down_buf):
        command.downgrade(pg_cfg, "20261205_0038:20261204_0037", sql=True)
    down_sql = down_buf.getvalue()
    assert "DROP INDEX uq_session_files_extracted_text_key" in down_sql
    assert "DROP CONSTRAINT ck_session_files_extraction_consistency" in down_sql
    assert "DROP CONSTRAINT ck_session_files_extracted_text_bytes" in down_sql
    assert "DROP CONSTRAINT ck_session_files_extraction_status" in down_sql
    assert "DROP COLUMN extraction_error_code" in down_sql
    assert "DROP COLUMN extracted_text_bytes" in down_sql
    assert "DROP COLUMN extracted_text_key" in down_sql
    assert "DROP COLUMN extraction_status" in down_sql

    # 2. 使用同步标准库 sqlite3 + Alembic Operations.context 验证升级 -> 一致性约束 -> 完整降级
    migration_0038_path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "20261205_0038_session_file_extraction_cache.py"
    )
    spec = importlib.util.spec_from_file_location("migration_20261205_0038", migration_0038_path)
    assert spec is not None and spec.loader is not None
    migration_0038: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration_0038)

    db_path = tmp_path / "m3_1_migration.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id VARCHAR(64) NOT NULL PRIMARY KEY,
            owner_id VARCHAR(128) NOT NULL,
            UNIQUE (id, owner_id)
        );
        CREATE TABLE run_tool_calls (id VARCHAR(128) NOT NULL PRIMARY KEY);
        CREATE TABLE session_files (
            id VARCHAR(128) NOT NULL PRIMARY KEY,
            session_id VARCHAR(64) NOT NULL,
            owner_id VARCHAR(128) NOT NULL,
            source VARCHAR(16) NOT NULL,
            filename VARCHAR(255) NOT NULL,
            label VARCHAR(255),
            mime_type VARCHAR(255) NOT NULL,
            size_bytes BIGINT NOT NULL,
            sha256 VARCHAR(64) NOT NULL,
            object_key VARCHAR(1024) NOT NULL,
            source_call_id VARCHAR(128),
            output_index INTEGER,
            idempotency_key VARCHAR(128),
            request_fingerprint VARCHAR(64),
            created_at DATETIME NOT NULL,
            CONSTRAINT ck_session_files_source CHECK (source IN ('upload', 'artifact')),
            CONSTRAINT ck_session_files_size CHECK (size_bytes >= 0),
            CONSTRAINT uq_session_files_session_id UNIQUE (session_id, id),
            CONSTRAINT uq_session_files_object_key UNIQUE (object_key),
            CONSTRAINT fk_session_files_session_owner
                FOREIGN KEY(session_id, owner_id)
                REFERENCES sessions (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_session_files_source_call
                FOREIGN KEY(source_call_id) REFERENCES run_tool_calls (id) ON DELETE RESTRICT
        );
        CREATE INDEX ix_session_files_session_created
            ON session_files (session_id, created_at, id);
        """
    )
    conn.close()

    sync_engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with sync_engine.begin() as sa_conn:
            with Operations.context(MigrationContext.configure(sa_conn)):
                migration_0038.upgrade()

        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO sessions VALUES ('s1', 'u1')")
        conn.execute(
            """
            INSERT INTO session_files (
                id, session_id, owner_id, source, filename, mime_type, size_bytes,
                sha256, object_key, idempotency_key, request_fingerprint, created_at
            ) VALUES (
                'f1', 's1', 'u1', 'upload', 'a.pdf', 'application/pdf', 100,
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'session-files/aa/f1', 'k1', 'fp1', '2026-11-24 00:00:00'
            )
            """
        )
        # 违反组合约束（ready 缺少 key/bytes）必须被拦截
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE session_files SET extraction_status = 'ready' WHERE id = 'f1'")
        # 违反组合约束（failed 缺少 error_code）必须被拦截
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE session_files SET extraction_status = 'failed' WHERE id = 'f1'")
        # 合法 ready 状态通过
        conn.execute(
            """
            UPDATE session_files
            SET extraction_status = 'ready',
                extracted_text_key = 'session-files-extracted/aa/f1.txt',
                extracted_text_bytes = 64,
                extraction_error_code = NULL
            WHERE id = 'f1'
            """
        )
        # 合法 failed 状态通过
        conn.execute(
            """
            UPDATE session_files
            SET extraction_status = 'failed',
                extracted_text_key = NULL,
                extracted_text_bytes = NULL,
                extraction_error_code = 'document_no_extractable_text'
            WHERE id = 'f1'
            """
        )
        conn.commit()
        conn.close()

        with sync_engine.begin() as sa_conn:
            with Operations.context(MigrationContext.configure(sa_conn)):
                migration_0038.downgrade()

        conn = sqlite3.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(session_files)").fetchall()}
        assert not cols.intersection(
            {
                "extraction_status",
                "extracted_text_key",
                "extracted_text_bytes",
                "extraction_error_code",
            }
        )
        conn.close()
    finally:
        sync_engine.dispose()
    # 确认 sqlalchemy text 导出在无告警情况下可用
    assert str(text("SELECT 1")) == "SELECT 1"
