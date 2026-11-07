"""Local parsers for supported knowledge source formats."""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path, PurePath
from typing import Any

from domain.knowledge.model import ChunkLocator, TextBlock
from domain.knowledge.ports import ParsedDocument


class LocalDocumentParser:
    """Extract searchable text and stable source locators from local documents."""

    def __init__(
        self, *, min_pdf_chars: int = 24, ocr_languages: str = "en", ocr_engine: Any | None = None
    ) -> None:
        self._min_pdf_chars = min_pdf_chars
        self._ocr_languages = ocr_languages
        self._ocr: Any = ocr_engine

    async def parse(self, filename: str, content: bytes) -> ParsedDocument:
        return await asyncio.to_thread(self._parse_bounded, filename, content)

    def _parse_bounded(self, filename: str, content: bytes) -> ParsedDocument:
        document = self._parse(filename, content)
        if sum(len(block.text) for block in document.blocks) > 10_000_000:
            raise ValueError("knowledge_extracted_text_limit")
        return document

    def validate_file(
        self, filename: str, path: Path, *, max_bytes: int = 20 * 1024 * 1024
    ) -> None:
        """Validate cheap file signatures and archive/image limits before persistence."""
        suffix = PurePath(filename).suffix.lower()
        if not path.is_file() or path.stat().st_size > max_bytes:
            raise ValueError("knowledge_file_size_invalid")
        with path.open("rb") as source:
            signature = source.read(16)
        if suffix in {".txt", ".md"}:
            try:
                path.read_text(encoding="utf-8-sig")
            except (UnicodeDecodeError, OSError) as error:
                raise ValueError("knowledge_text_encoding_unsupported") from error
            if path.stat().st_size > 10_000_000:
                raise ValueError("knowledge_extracted_text_limit")
            return
        if suffix == ".pdf":
            if not signature.startswith(b"%PDF-"):
                raise ValueError("knowledge_file_signature_invalid")
            import pypdfium2 as pdfium  # type: ignore[import-untyped]

            pdf = pdfium.PdfDocument(str(path))
            try:
                if len(pdf) > 500:
                    raise ValueError("knowledge_pdf_page_limit")
            finally:
                pdf.close()
            return
        if suffix in {".docx", ".xlsx", ".pptx"}:
            self._validate_office_archive(path, suffix)
            return
        if suffix in {".png", ".jpg", ".jpeg"}:
            from PIL import Image

            try:
                with Image.open(path) as image:
                    if image.format not in ({"PNG"} if suffix == ".png" else {"JPEG"}):
                        raise ValueError("knowledge_file_signature_invalid")
                    if image.width * image.height > 40_000_000:
                        raise ValueError("knowledge_image_too_large")
                    image.verify()
            except (OSError, SyntaxError) as error:
                raise ValueError("knowledge_image_invalid") from error
            return
        if suffix in {".doc", ".xls", ".ppt"}:
            raise ValueError("knowledge_legacy_office_unsupported")
        raise ValueError("knowledge_file_type_unsupported")

    @staticmethod
    def _validate_office_archive(path: Path, suffix: str) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if len(members) > 10_000:
                    raise ValueError("knowledge_archive_member_limit")
                if sum(member.file_size for member in members) > 200 * 1024 * 1024:
                    raise ValueError("knowledge_archive_expanded_size_limit")
                if any(member.flag_bits & 0x1 for member in members):
                    raise ValueError("knowledge_archive_encrypted")
                names = {member.filename.lower() for member in members}
                if any(
                    "vbaproject" in name or name.endswith((".xlsm", ".docm", ".pptm"))
                    for name in names
                ) or any(
                    "macroenabled" in archive.read(member).decode("utf-8", "ignore").lower()
                    for member in members
                    if member.filename.lower() == "[content_types].xml"
                ):
                    raise ValueError("knowledge_office_macro_unsupported")
                if "[content_types].xml" not in names:
                    raise ValueError("knowledge_file_signature_invalid")
                required_members = {
                    ".docx": {"word/document.xml"},
                    ".xlsx": {"xl/workbook.xml"},
                    ".pptx": {"ppt/presentation.xml"},
                }.get(suffix, set())
                if not required_members.issubset(names):
                    raise ValueError("knowledge_file_signature_invalid")
        except zipfile.BadZipFile as error:
            raise ValueError("knowledge_office_archive_invalid") from error

    def _parse(self, filename: str, content: bytes) -> ParsedDocument:
        suffix = PurePath(filename).suffix.lower()
        if suffix in {".txt", ".md"}:
            try:
                text = content.decode("utf-8-sig")
            except UnicodeDecodeError as error:
                raise ValueError("knowledge_text_encoding_unsupported") from error
            kind = "markdown" if suffix == ".md" else "text"
            return ParsedDocument(
                tuple(
                    TextBlock(line, ChunkLocator(kind=kind, start_line=number, end_line=number))
                    for number, line in enumerate(text.splitlines(), 1)
                    if line.strip()
                )
            )
        if suffix == ".pdf":
            return self._pdf(content)
        if suffix == ".docx":
            return self._docx(content)
        if suffix == ".xlsx":
            return self._xlsx(content)
        if suffix == ".pptx":
            return self._pptx(content)
        if suffix in {".png", ".jpg", ".jpeg"}:
            return ParsedDocument(self._ocr_image(content, media_id="image"))
        if suffix in {".doc", ".xls", ".ppt"}:
            raise ValueError("knowledge_legacy_office_unsupported")
        raise ValueError("knowledge_file_type_unsupported")

    def _ocr_engine(self) -> Any:
        if self._ocr is None:
            try:
                from paddleocr import PaddleOCR  # type: ignore[import-untyped]
            except ImportError as error:
                raise RuntimeError("knowledge_ocr_engine_unavailable") from error
            self._ocr = PaddleOCR(
                lang=self._ocr_languages,
                text_detection_model_name="PP-OCRv6_medium_det",
                text_recognition_model_name="PP-OCRv6_medium_rec",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        return self._ocr

    def _ocr_image(
        self, image: bytes, *, page: int | None = None, media_id: str | None = None
    ) -> tuple[TextBlock, ...]:
        from PIL import Image

        with Image.open(io.BytesIO(image)) as decoded:
            if decoded.width * decoded.height > 40_000_000:
                raise ValueError("knowledge_image_too_large")
            import numpy as np

            pixels = np.asarray(decoded.convert("RGB"))
            result = self._ocr_engine().predict(input=pixels)
        texts: list[str] = []
        for item in result:
            payload = item.json.get("res", {}) if hasattr(item, "json") else {}
            for line in payload.get("rec_texts", []):
                value = str(line).strip()
                if value and value not in texts:
                    texts.append(value)
        text = "\n".join(texts).strip()
        if not text:
            return ()
        return (TextBlock(text, ChunkLocator(kind="ocr", page=page, media_id=media_id)),)

    def _pdf(self, content: bytes) -> ParsedDocument:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(content)
        blocks: list[TextBlock] = []
        try:
            if len(pdf) > 500:
                raise ValueError("knowledge_pdf_page_limit")
            for number in range(len(pdf)):
                page = pdf[number]
                try:
                    textpage = page.get_textpage()
                    text = textpage.get_text_range().strip()
                    if text:
                        blocks.append(TextBlock(text, ChunkLocator(kind="pdf", page=number + 1)))
                    seen_ocr: set[str] = set()
                    image_no = 0
                    image_ocr_succeeded = False
                    for image_object in page.get_objects():
                        if not hasattr(image_object, "get_bitmap"):
                            continue
                        image_no += 1
                        bitmap = image_object.get_bitmap()
                        image_buffer = io.BytesIO()
                        bitmap.to_pil().save(image_buffer, format="PNG")
                        ocr_blocks = self._ocr_image(
                            image_buffer.getvalue(),
                            page=number + 1,
                            media_id=f"pdf-page-{number + 1}-image-{image_no}",
                        )
                        image_ocr_succeeded = image_ocr_succeeded or bool(ocr_blocks)
                        for block in ocr_blocks:
                            normalized = " ".join(block.text.casefold().split())
                            if (
                                normalized
                                and normalized not in seen_ocr
                                and normalized not in text.casefold()
                            ):
                                blocks.append(block)
                                seen_ocr.add(normalized)
                    if len(text) < self._min_pdf_chars and not image_ocr_succeeded:
                        bitmap = page.render(scale=2).to_pil()
                        buffer = io.BytesIO()
                        bitmap.save(buffer, format="PNG")
                        blocks.extend(
                            self._ocr_image(
                                buffer.getvalue(),
                                page=number + 1,
                                media_id=f"pdf-page-{number + 1}",
                            )
                        )
                finally:
                    page.close()
        finally:
            pdf.close()
        if not blocks:
            raise ValueError("knowledge_document_text_empty")
        return ParsedDocument(tuple(blocks))

    def _docx(self, content: bytes) -> ParsedDocument:
        from docx import Document

        doc = Document(io.BytesIO(content))
        blocks: list[TextBlock] = []
        for i, paragraph in enumerate(doc.paragraphs, 1):
            if paragraph.text.strip():
                blocks.append(
                    TextBlock(
                        paragraph.text,
                        ChunkLocator(
                            kind="docx",
                            start_line=i,
                            heading=paragraph.style.name if paragraph.style else None,
                        ),
                    )
                )
        for ti, table in enumerate(doc.tables, 1):
            for ri, row in enumerate(table.rows, 1):
                text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if text:
                    blocks.append(
                        TextBlock(
                            text,
                            ChunkLocator(kind="docx_table", start_line=ri, heading=f"table {ti}"),
                            "table",
                        )
                    )
        seen: set[str] = set()
        for rel in doc.part.rels.values():
            if "image" in rel.reltype and rel.target_ref not in seen:
                seen.add(rel.target_ref)
                blocks.extend(self._ocr_image(rel.target_part.blob, media_id=rel.target_ref))
        return ParsedDocument(tuple(blocks))

    def _xlsx(self, content: bytes) -> ParsedDocument:
        from openpyxl import load_workbook  # type: ignore[import-untyped]

        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
        value_workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        blocks: list[TextBlock] = []
        warnings: list[str] = []
        try:
            count = 0
            for sheet in workbook.worksheets:
                value_sheet = value_workbook[sheet.title]
                value_rows = iter(value_sheet.iter_rows())
                for row in sheet.iter_rows():
                    value_row = next(value_rows, ())
                    count += len(row)
                    if count > 1_000_000:
                        raise ValueError("knowledge_spreadsheet_cell_limit")
                    values: list[str] = []
                    for cell, cached in zip(row, value_row, strict=False):
                        value = cell.value
                        if isinstance(value, str) and value.startswith("="):
                            if cached.value is not None:
                                value = cached.value
                            else:
                                warnings.append("xlsx_formula_cache_missing")
                        if value is not None:
                            values.append(f"{cell.coordinate}: {value}")
                    if values:
                        row_number = next(cell.row for cell in row if hasattr(cell, "row"))
                        blocks.append(
                            TextBlock(
                                " | ".join(values),
                                ChunkLocator(
                                    kind="xlsx",
                                    sheet=sheet.title,
                                    cell_range=f"{row_number}:{row_number}",
                                ),
                            )
                        )
        finally:
            workbook.close()
            value_workbook.close()
        # Image drawings are unavailable in read_only mode, so inspect them in a
        # second bounded open while preserving streaming row extraction above.
        image_workbook = load_workbook(io.BytesIO(content), read_only=False, data_only=True)
        try:
            for sheet in image_workbook.worksheets:
                for image_no, image in enumerate(getattr(sheet, "_images", ()), 1):
                    anchor = image.anchor._from
                    cell = f"{anchor.col + 1}:{anchor.row + 1}"
                    blocks.extend(
                        self._ocr_image(
                            image._data(),
                            media_id=f"sheet-{sheet.title}-image-{image_no}-cell-{cell}",
                        )
                    )
        finally:
            image_workbook.close()
        return ParsedDocument(tuple(blocks), tuple(dict.fromkeys(warnings)))

    def _pptx(self, content: bytes) -> ParsedDocument:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        presentation = Presentation(io.BytesIO(content))
        blocks: list[TextBlock] = []
        for slide_no, slide in enumerate(presentation.slides, 1):
            for index, shape in enumerate(slide.shapes, 1):
                blocks.extend(self._pptx_shape(shape, slide_no, str(index), MSO_SHAPE_TYPE))
        return ParsedDocument(tuple(blocks))

    def _pptx_shape(
        self, shape: Any, slide_no: int, path: str, shape_types: Any
    ) -> list[TextBlock]:
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        blocks: list[TextBlock] = []
        if shape.has_text_frame and shape.text.strip():
            blocks.append(
                TextBlock(shape.text, ChunkLocator(kind="pptx", slide=slide_no, shape=path))
            )
        if shape.has_table:
            for row in shape.table.rows:
                text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if text:
                    blocks.append(
                        TextBlock(
                            text,
                            ChunkLocator(kind="pptx_table", slide=slide_no, shape=path),
                            "table",
                        )
                    )
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            blocks.extend(
                self._ocr_image(shape.image.blob, media_id=f"slide-{slide_no}-shape-{path}")
            )
        elif shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            for index, child in enumerate(shape.shapes, 1):
                blocks.extend(self._pptx_shape(child, slide_no, f"{path}.{index}", shape_types))
        return blocks
