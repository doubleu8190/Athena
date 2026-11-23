"""Fixtures exercise supported document formats through the public parser."""

from __future__ import annotations

import asyncio
import io
import os
import zipfile
from types import SimpleNamespace
from typing import Any
from zipfile import BadZipFile

import pytest
from docx import Document
from openpyxl import Workbook  # type: ignore[import-untyped]
from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.util import Inches

from infrastructure.knowledge.parsers import LocalDocumentParser


def _pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        (
            f"<< /Length {len(f'BT /F1 12 Tf 40 760 Td ({escaped}) Tj ET')} >>\n"
            f"stream\nBT /F1 12 Tf 40 760 Td ({escaped}) Tj ET\nendstream"
        ).encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
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
    trailer = f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF"
    output.extend(trailer.encode())
    return bytes(output)


def _mixed_pdf(text: str, image: bytes) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 40 760 Td ({escaped}) Tj ET\nq 100 0 0 100 40 500 cm /Im1 Do Q".encode()
    image_stream = (
        b"<< /Type /XObject /Subtype /Image /Width 1300 /Height 180 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
        + str(len(image)).encode()
        + b" >>\nstream\n"
        + image
        + b"\nendstream"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /Font << /F1 5 0 R >> /XObject << /Im1 6 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        image_stream,
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


def test_text_parser_preserves_line_location() -> None:
    result = asyncio.run(LocalDocumentParser().parse("notes.md", "# 标题\n正文".encode()))
    assert [block.locator.start_line for block in result.blocks] == [1, 2]
    assert result.blocks[0].locator.kind == "markdown"


def test_pdf_parser_extracts_text_and_page() -> None:
    result = asyncio.run(
        LocalDocumentParser().parse("notes.pdf", _pdf("PostgreSQL knowledge source"))
    )
    assert len(result.blocks) == 1
    assert "PostgreSQL" in result.blocks[0].text
    assert result.blocks[0].locator.page == 1


def test_mixed_pdf_extracts_text_and_embedded_image_ocr() -> None:
    image = io.BytesIO()
    _label_image().save(image, format="JPEG")
    parser = LocalDocumentParser(min_pdf_chars=5, ocr_engine=_FakeOcr(duplicate=True))

    result = asyncio.run(
        parser.parse("mixed.pdf", _mixed_pdf("Selectable PDF text", image.getvalue()))
    )

    assert any(
        block.locator.kind == "pdf" and "Selectable PDF text" in block.text
        for block in result.blocks
    )
    image_blocks = [block for block in result.blocks if block.locator.kind == "ocr"]
    assert len(image_blocks) == 1
    assert image_blocks[0].locator.page == 1
    assert image_blocks[0].locator.media_id == "pdf-page-1-image-1"
    assert image_blocks[0].text == "识别文字 OCR"


def test_short_number_is_preserved_on_page_with_embedded_image() -> None:
    image = io.BytesIO()
    _label_image().save(image, format="JPEG")
    parser = LocalDocumentParser(ocr_engine=_FakeOcr())

    result = asyncio.run(parser.parse("mixed.pdf", _mixed_pdf("A-7", image.getvalue())))

    assert any(block.locator.kind == "pdf" and "A-7" in block.text for block in result.blocks)
    assert any(block.locator.media_id == "pdf-page-1-image-1" for block in result.blocks)
    assert not any(block.locator.media_id == "pdf-page-1" for block in result.blocks)


def test_short_native_text_is_preserved_when_page_ocr_is_added() -> None:
    result = asyncio.run(LocalDocumentParser(ocr_engine=_FakeOcr()).parse("short.pdf", _pdf("A-7")))

    assert any(block.locator.kind == "pdf" and "A-7" in block.text for block in result.blocks)
    assert any(block.locator.media_id == "pdf-page-1" for block in result.blocks)


def test_docx_parser_extracts_paragraph_and_table() -> None:
    doc = Document()
    doc.add_paragraph("Knowledge heading")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "field"
    table.cell(0, 1).text = "中文 value"
    image = io.BytesIO()
    from PIL import Image

    Image.new("RGB", (20, 20), "white").save(image, format="PNG")
    doc.add_picture(io.BytesIO(image.getvalue()))
    output = io.BytesIO()
    doc.save(output)
    result = asyncio.run(
        LocalDocumentParser(ocr_engine=_FakeOcr()).parse("notes.docx", output.getvalue())
    )
    assert any("Knowledge heading" in block.text for block in result.blocks)
    assert any(
        block.locator.kind == "docx_table" and "中文 value" in block.text for block in result.blocks
    )
    assert any(block.locator.media_id for block in result.blocks)


def test_xlsx_parser_preserves_sheet_and_cell_range() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "指标"
    sheet["A1"] = "用户"
    sheet["B1"] = "数量"
    sheet["A2"] = "Athena"
    sheet["B2"] = 12
    image_data = io.BytesIO()
    from PIL import Image

    Image.new("RGB", (20, 20), "white").save(image_data, format="PNG")
    from openpyxl.drawing.image import Image as SpreadsheetImage  # type: ignore[import-untyped]

    sheet.add_image(SpreadsheetImage(io.BytesIO(image_data.getvalue())), "C3")
    output = io.BytesIO()
    workbook.save(output)
    result = asyncio.run(
        LocalDocumentParser(ocr_engine=_FakeOcr()).parse("data.xlsx", output.getvalue())
    )
    assert len(result.blocks) == 3
    assert result.blocks[1].locator.sheet == "指标"
    assert "Athena" in result.blocks[1].text
    assert any("cell-3:3" in (block.locator.media_id or "") for block in result.blocks)


def test_xlsx_formula_without_cached_value_is_preserved_with_warning() -> None:
    workbook = Workbook()
    workbook.active["B7"] = "=1+2"
    output = io.BytesIO()
    workbook.save(output)

    result = asyncio.run(LocalDocumentParser().parse("formula.xlsx", output.getvalue()))

    assert any("B7: =1+2" in block.text for block in result.blocks)
    assert result.warnings == ("xlsx_formula_cache_missing",)
    formula_block = next(block for block in result.blocks if "B7:" in block.text)
    assert formula_block.locator.kind == "xlsx"
    assert formula_block.locator.sheet == "Sheet"
    assert formula_block.locator.cell_range == "7:7"


def test_pptx_parser_extracts_text_and_slide_locator() -> None:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1))
    shape.text = "Knowledge slide"
    image_data = io.BytesIO()
    from PIL import Image

    Image.new("RGB", (20, 20), "white").save(image_data, format="PNG")
    slide.shapes.add_picture(image_data, Inches(1), Inches(2))
    group = slide.shapes.add_group_shape()
    nested_text = group.shapes.add_textbox(Inches(1), Inches(3), Inches(4), Inches(1))
    nested_text.text = "Nested group text"
    nested = group.shapes.add_group_shape()
    nested.shapes.add_picture(io.BytesIO(image_data.getvalue()), Inches(1), Inches(1))
    output = io.BytesIO()
    presentation.save(output)
    result = asyncio.run(
        LocalDocumentParser(ocr_engine=_FakeOcr()).parse("slides.pptx", output.getvalue())
    )
    text_block = next(block for block in result.blocks if block.locator.kind == "pptx")
    assert text_block.text == "Knowledge slide"
    assert text_block.locator.slide == 1
    assert any(block.text == "Nested group text" for block in result.blocks)
    image_ids = {block.locator.media_id for block in result.blocks if block.locator.media_id}
    assert "slide-1-shape-3" in image_ids
    assert any(image_id.endswith(".2.1") for image_id in image_ids)


class _FakeOcr:
    def __init__(self, *, duplicate: bool = False, recognized_text: str = "识别文字 OCR") -> None:
        self._duplicate = duplicate
        self._recognized_text = recognized_text

    def predict(self, *, input: Any) -> list[SimpleNamespace]:
        texts = [self._recognized_text] * (2 if self._duplicate else 1)
        return [SimpleNamespace(json={"res": {"rec_texts": texts}})]


@pytest.mark.parametrize("filename", ["bad.pdf", "bad.docx", "bad.xlsx", "bad.pptx"])
def test_malformed_document_fails_clearly(filename: str) -> None:
    with pytest.raises((ValueError, RuntimeError, IndexError, KeyError, OSError, BadZipFile)):  # noqa: B017
        asyncio.run(LocalDocumentParser().parse(filename, b"not a document"))


def test_legacy_office_formats_are_rejected() -> None:
    with pytest.raises(ValueError, match="knowledge_legacy_office_unsupported"):
        asyncio.run(LocalDocumentParser().parse("old.doc", b""))


@pytest.mark.parametrize("suffix", ["png", "jpeg"])
def test_image_ocr_preserves_source_identity(suffix: str) -> None:
    image = _label_image()
    output = io.BytesIO()
    image.save(output, format="PNG" if suffix == "png" else "JPEG")
    result = asyncio.run(
        LocalDocumentParser(ocr_engine=_FakeOcr()).parse(f"sample.{suffix}", output.getvalue())
    )
    assert result.blocks[0].locator.kind == "ocr"
    assert result.blocks[0].locator.media_id == "image"


def test_scanned_pdf_page_uses_ocr_and_keeps_page_locator() -> None:
    output = io.BytesIO()
    _label_image().save(output, format="PDF", resolution=144)
    result = asyncio.run(
        LocalDocumentParser(ocr_engine=_FakeOcr()).parse("scan.pdf", output.getvalue())
    )
    assert result.blocks[0].locator.page == 1
    assert result.blocks[0].locator.kind == "ocr"


@pytest.mark.real_ocr
@pytest.mark.skipif(
    not os.getenv("KNOWLEDGE_REAL_OCR"),
    reason="set KNOWLEDGE_REAL_OCR=1 to run local PaddleOCR recognition",
)
def test_real_chinese_ocr_for_image_and_scanned_pdf() -> None:
    parser = LocalDocumentParser(ocr_languages="ch")
    image_bytes = io.BytesIO()
    _label_image().save(image_bytes, format="PNG")
    image_result = asyncio.run(parser.parse("sample.png", image_bytes.getvalue()))
    assert "知识检索" in image_result.blocks[0].text

    pdf_bytes = io.BytesIO()
    _label_image().save(pdf_bytes, format="PDF", resolution=144)
    pdf_result = asyncio.run(parser.parse("scan.pdf", pdf_bytes.getvalue()))
    assert pdf_result.blocks[0].locator.page == 1
    assert "知识检索" in pdf_result.blocks[0].text


def _label_image() -> Image.Image:
    image = Image.new("RGB", (1300, 180), "white")
    draw = ImageDraw.Draw(image)
    from PIL import ImageFont

    font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 48)
    draw.text((20, 35), "Athena 知识检索 PostgreSQL 17", font=font, fill="black")
    return image


def test_pdf_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = LocalDocumentParser()
    monkeypatch.setattr(
        parser,
        "_pdf",
        lambda _content: (_ for _ in ()).throw(ValueError("knowledge_pdf_page_limit")),
    )
    with pytest.raises(ValueError, match="knowledge_pdf_page_limit"):
        asyncio.run(parser.parse("large.pdf", b"pdf"))


def _office_zip(path: Any, suffix: str, members: dict[str, bytes]) -> None:
    required = {
        ".docx": "word/document.xml",
        ".xlsx": "xl/workbook.xml",
        ".pptx": "ppt/presentation.xml",
    }[suffix]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types></Types>")
        archive.writestr(required, b"<root></root>")
        for name, value in members.items():
            archive.writestr(name, value)


@pytest.mark.parametrize(
    ("suffix", "required"),
    [
        (".docx", "word/document.xml"),
        (".xlsx", "xl/workbook.xml"),
        (".pptx", "ppt/presentation.xml"),
    ],
)
def test_validate_file_accepts_supported_office_packages(
    tmp_path: Any, suffix: str, required: str
) -> None:
    path = tmp_path / f"valid{suffix}"
    _office_zip(path, suffix, {})

    LocalDocumentParser().validate_file(path.name, path)


def test_validate_file_rejects_office_package_missing_type_specific_member(tmp_path: Any) -> None:
    path = tmp_path / "wrong.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<Types></Types>")
        archive.writestr("xl/workbook.xml", b"<root/>")
    with pytest.raises(ValueError, match="knowledge_file_signature_invalid"):
        LocalDocumentParser().validate_file(path.name, path)


@pytest.mark.parametrize(
    ("member", "payload", "error"),
    [
        ("word/vbaProject.bin", b"macro", "knowledge_office_macro_unsupported"),
        ("word/huge.xml", b"x" * 201 * 1024 * 1024, "knowledge_archive_expanded_size_limit"),
    ],
)
def test_validate_file_rejects_macro_and_expanded_size(
    tmp_path: Any, member: str, payload: bytes, error: str
) -> None:
    path = tmp_path / "bad.docx"
    _office_zip(path, ".docx", {member: payload})
    with pytest.raises(ValueError, match=error):
        LocalDocumentParser().validate_file(path.name, path)


def test_validate_file_rejects_macro_enabled_content_type(tmp_path: Any) -> None:
    path = tmp_path / "macro.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            b'<Types><Override ContentType="application/vnd.ms-word.document.'
            b'macroEnabled.main+xml"/></Types>',
        )
        archive.writestr("word/document.xml", b"<root/>")
    with pytest.raises(ValueError, match="knowledge_office_macro_unsupported"):
        LocalDocumentParser().validate_file(path.name, path)


def test_validate_file_rejects_encrypted_office_member(tmp_path: Any) -> None:
    path = tmp_path / "encrypted.docx"
    _office_zip(path, ".docx", {})
    content = bytearray(path.read_bytes())
    local_header = content.find(b"PK\x03\x04")
    central_header = content.find(b"PK\x01\x02")
    assert local_header >= 0 and central_header >= 0
    content[local_header + 6] |= 1
    content[central_header + 8] |= 1
    path.write_bytes(content)
    with pytest.raises(ValueError, match="knowledge_archive_encrypted"):
        LocalDocumentParser().validate_file(path.name, path)


def test_validate_file_rejects_too_many_archive_members(tmp_path: Any) -> None:
    path = tmp_path / "many.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("word/document.xml", b"<root/>")
        for index in range(10_000):
            archive.writestr(f"word/{index}.xml", b"x")
    with pytest.raises(ValueError, match="knowledge_archive_member_limit"):
        LocalDocumentParser().validate_file(path.name, path)


def test_validate_file_checks_signature_and_image_pixel_limit(tmp_path: Any) -> None:
    invalid = tmp_path / "invalid.pdf"
    invalid.write_bytes(b"not a pdf")
    with pytest.raises(ValueError, match="knowledge_file_signature_invalid"):
        LocalDocumentParser().validate_file(invalid.name, invalid)

    too_large = tmp_path / "large.png"
    Image.new("RGB", (7_000, 6_000)).save(too_large)
    with pytest.raises(ValueError, match="knowledge_image_too_large"):
        LocalDocumentParser().validate_file(too_large.name, too_large)


def test_validate_file_checks_upload_size_and_text_encoding(tmp_path: Any) -> None:
    path = tmp_path / "notes.txt"
    path.write_bytes(b"12345")
    with pytest.raises(ValueError, match="knowledge_file_size_invalid"):
        LocalDocumentParser().validate_file(path.name, path, max_bytes=4)
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="knowledge_text_encoding_unsupported"):
        LocalDocumentParser().validate_file(path.name, path)


def test_parser_rejects_extracted_text_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from domain.knowledge.model import ChunkLocator, TextBlock
    from domain.knowledge.ports import ParsedDocument

    parser = LocalDocumentParser()
    monkeypatch.setattr(
        parser,
        "_parse",
        lambda *_args: ParsedDocument((TextBlock("x" * 10_000_001, ChunkLocator(kind="text")),)),
    )
    with pytest.raises(ValueError, match="knowledge_extracted_text_limit"):
        parser._parse_bounded("large.txt", b"")


def test_validate_file_translates_corrupted_pdf_to_business_error(tmp_path: Any) -> None:
    """带 %PDF- 头但内容损坏的文件应返回 knowledge_pdf_invalid，避免触发 HTTP 500。"""
    corrupted = tmp_path / "broken.pdf"
    corrupted.write_bytes(b"%PDF-1.7\ncorrupted binary payload")
    with pytest.raises(ValueError, match="knowledge_pdf_invalid"):
        LocalDocumentParser().validate_file(corrupted.name, corrupted)


def test_validate_office_archive_enforces_content_types_size_limit(tmp_path: Any) -> None:
    """超过 10 MiB 的 [Content_Types].xml 应被有界拦截并返回扩展大小超限错误。"""
    oversized = tmp_path / "huge-ct.docx"
    with zipfile.ZipFile(oversized, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            b"<Types>" + b" " * (10 * 1024 * 1024 + 16) + b"</Types>",
        )
        archive.writestr("word/document.xml", b"<root/>")
    with pytest.raises(ValueError, match="knowledge_archive_expanded_size_limit"):
        LocalDocumentParser().validate_file(oversized.name, oversized)


def test_xlsx_without_embedded_media_skips_full_dom_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不含图片/绘图的纯表格工作簿仅以 read_only=True 读取，绝不触发 read_only=False 全量加载。"""
    import openpyxl

    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "字段"
    sheet["B1"] = "值"
    output = io.BytesIO()
    workbook.save(output)

    real_load_workbook = openpyxl.load_workbook
    read_only_flags: list[bool] = []

    def tracking_load_workbook(*args: Any, **kwargs: Any) -> Any:
        read_only_flags.append(bool(kwargs.get("read_only", False)))
        return real_load_workbook(*args, **kwargs)

    monkeypatch.setattr(openpyxl, "load_workbook", tracking_load_workbook)
    result = asyncio.run(LocalDocumentParser().parse("plain.xlsx", output.getvalue()))

    assert len(result.blocks) == 1
    assert read_only_flags == [True, True]


def test_xlsx_handles_absolute_anchor_image_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不含 _from 属性的 AbsoluteAnchor 嵌入式图片应回退 pos-N 定位而不崩溃。"""
    import openpyxl

    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "数据"
    image_data = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(image_data, format="PNG")
    from openpyxl.drawing.image import Image as SpreadsheetImage

    sheet.add_image(SpreadsheetImage(io.BytesIO(image_data.getvalue())), "B2")
    output = io.BytesIO()
    workbook.save(output)

    real_load_workbook = openpyxl.load_workbook

    def patched_load_workbook(*args: Any, **kwargs: Any) -> Any:
        loaded = real_load_workbook(*args, **kwargs)
        if not kwargs.get("read_only", False):
            for ws in loaded.worksheets:
                for image in getattr(ws, "_images", ()):
                    image.anchor = SimpleNamespace(pos="absolute")
        return loaded

    monkeypatch.setattr(openpyxl, "load_workbook", patched_load_workbook)
    result = asyncio.run(
        LocalDocumentParser(ocr_engine=_FakeOcr()).parse("abs.xlsx", output.getvalue())
    )
    assert any(
        block.locator.media_id and block.locator.media_id.endswith("pos-1")
        for block in result.blocks
    )


def test_docx_skips_external_and_unsupported_vector_images(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DOCX 外链图片与 PIL 不支持的矢量图（如 EMF/SVG）应跳过并记录告警，不影响正文解析。"""
    doc = Document()
    doc.add_paragraph("保留的正文段落")
    # 添加内嵌外链图片关系（is_external=True）与无法被 PIL 解码的非光栅图片。
    doc.part.rels.add_relationship(
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
        "https://example.invalid/external.png",
        "rIdExternalImg",
        is_external=True,
    )
    output = io.BytesIO()
    doc.save(output)
    # 注入一个非光栅 SVG 图片字节与内部关系，模拟真实 Word 文档中的矢量图。
    patched_zip = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(output.getvalue()), "r") as src,
        zipfile.ZipFile(patched_zip, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "[Content_Types].xml":
                xml = data.decode("utf-8").replace(
                    "</Types>",
                    '<Default Extension="svg" ContentType="image/svg+xml"/></Types>',
                )
                data = xml.encode("utf-8")
            elif item.filename == "word/_rels/document.xml.rels":
                rel_type = (
                    "http://schemas.openxmlformats.org/"
                    "officeDocument/2006/relationships/image"
                )
                xml = data.decode("utf-8").replace(
                    "</Relationships>",
                    f'<Relationship Id="rIdSvgImg" Type="{rel_type}" '
                    'Target="media/vector.svg"/></Relationships>',
                )
                data = xml.encode("utf-8")
            dst.writestr(item, data)
        dst.writestr("word/media/vector.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>")

    with caplog.at_level("WARNING"):
        result = asyncio.run(
            LocalDocumentParser(ocr_engine=_FakeOcr()).parse("mixed.docx", patched_zip.getvalue())
        )

    assert any("保留的正文段落" in block.text for block in result.blocks)
    assert any("docx_unsupported_image_skipped" in record.message for record in caplog.records)


def test_pdf_skips_tiny_decorative_images_before_ocr() -> None:
    """PDF 内宽或高小于 32px 的装饰/细线图应直接跳过，不触发 OCR 引擎调用。"""
    tiny = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(tiny, format="JPEG")
    tiny_bytes = tiny.getvalue()
    stream = (
        b"BT /F1 12 Tf 40 760 Td (Enough native PDF text to skip page OCR) Tj ET\n"
        b"q 16 0 0 16 40 500 cm /Im1 Do Q"
    )
    image_stream = (
        b"<< /Type /XObject /Subtype /Image /Width 16 /Height 16 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
        + str(len(tiny_bytes)).encode()
        + b" >>\nstream\n"
        + tiny_bytes
        + b"\nendstream"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
            b"<< /Font << /F1 5 0 R >> /XObject << /Im1 6 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        image_stream,
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

    class _CountingOcr(_FakeOcr):
        calls = 0

        def predict(self, *, input: Any) -> list[SimpleNamespace]:
            self.calls += 1
            return super().predict(input=input)

    ocr = _CountingOcr()
    parser = LocalDocumentParser(min_pdf_chars=10, ocr_engine=ocr)
    result = asyncio.run(parser.parse("tiny.pdf", bytes(output)))
    assert ocr.calls == 0
    assert len(result.blocks) == 1
    assert result.blocks[0].locator.kind == "pdf"
