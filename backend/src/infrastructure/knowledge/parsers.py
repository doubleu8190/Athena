"""Local parsers for supported knowledge source formats."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import threading
import zipfile
from pathlib import Path, PurePath
from typing import Any

from common.office_archive import OfficeArchiveError, validate_office_archive
from domain.knowledge.model import ChunkLocator, TextBlock
from domain.knowledge.ports import (
    DocumentFileValidator,
    DocumentParser,
    DocumentParserFactory,
    ParsedDocument,
)

# 解析器与工厂共用同一 OCR 默认语言，避免默认配置漂移。
DEFAULT_OCR_LANGUAGES = "en"
_MAX_CONTENT_TYPES_BYTES = 10 * 1024 * 1024
_MIN_PDF_OCR_IMAGE_DIMENSION = 32
_MAX_PDF_SEEN_IMAGE_HASHES = 2048
# PDFium 底层 C++ 库非线程安全；同进程内并发解析/校验多个 PDF 时必须串行化访问，
# 避免多线程同时调用 pypdfium2 导致死锁或未定义行为。
_PDFIUM_LOCK = threading.Lock()

logger = logging.getLogger(__name__)


class LocalDocumentParser(DocumentParser, DocumentFileValidator):
    """Extract searchable text and stable source locators from local documents."""

    def __init__(
        self,
        *,
        min_pdf_chars: int = 24,
        ocr_languages: str = DEFAULT_OCR_LANGUAGES,
        ocr_engine: Any | None = None,
        enable_ocr: bool = True,
    ) -> None:
        self._min_pdf_chars = min_pdf_chars
        self._ocr_languages = ocr_languages
        self._ocr: Any = ocr_engine
        self._enable_ocr = enable_ocr

    async def parse(self, filename: str, content: bytes) -> ParsedDocument:
        return await asyncio.to_thread(self._parse_bounded, filename, content)

    def parse_sync(self, filename: str, content: bytes) -> ParsedDocument:
        """在调用方已管理线程边界时同步解析并执行文本上限校验。"""
        return self._parse_bounded(filename, content)

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

            with _PDFIUM_LOCK:
                try:
                    pdf = pdfium.PdfDocument(str(path))
                except ValueError:
                    raise
                except Exception as error:
                    # 将损坏或加密 PDF 触发的 Pdfium 底层异常转译为稳定业务错误码，
                    # 避免上传接口返回 HTTP 500。
                    raise ValueError("knowledge_pdf_invalid") from error
                try:
                    try:
                        page_count = len(pdf)
                    except ValueError:
                        raise
                    except Exception as error:
                        raise ValueError("knowledge_pdf_invalid") from error
                    if page_count > 500:
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
        required_marker = {
            ".docx": "word/document.xml",
            ".xlsx": "xl/workbook.xml",
            ".pptx": "ppt/presentation.xml",
        }.get(suffix)
        if required_marker is None:
            raise ValueError("knowledge_file_signature_invalid")
        error_code_map = {
            "bad_zip": "knowledge_office_archive_invalid",
            "unsafe_member_path": "knowledge_office_archive_invalid",
            "member_limit": "knowledge_archive_member_limit",
            "expanded_size_limit": "knowledge_archive_expanded_size_limit",
            "encrypted": "knowledge_archive_encrypted",
            "macro_unsupported": "knowledge_office_macro_unsupported",
            "signature_invalid": "knowledge_file_signature_invalid",
        }
        try:
            validate_office_archive(
                path,
                required_marker=required_marker,
                check_limits_and_macros=True,
                case_insensitive_markers=True,
                max_content_types_bytes=_MAX_CONTENT_TYPES_BYTES,
            )
        except OfficeArchiveError as error:
            raise ValueError(error_code_map[error.code]) from error

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
            if not self._enable_ocr:
                return ParsedDocument(())
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

    def _ocr_pil_image(
        self,
        pil_image: Any,
        *,
        page: int | None = None,
        media_id: str | None = None,
        _pixels: Any | None = None,
    ) -> tuple[TextBlock, ...]:
        """直接将 PIL Image 转为 RGB 数组送入 OCR 推理，跳过无意义的 PNG 编解码往返。"""
        # 关闭 OCR 时直接短路返回空元组，避免误加载重量级 PaddleOCR 引擎。
        if not self._enable_ocr:
            return ()
        if pil_image.width * pil_image.height > 40_000_000:
            raise ValueError("knowledge_image_too_large")
        pixels = _pixels
        if pixels is None:
            import numpy as np

            rgb_image = pil_image if pil_image.mode == "RGB" else pil_image.convert("RGB")
            try:
                pixels = np.ascontiguousarray(np.asarray(rgb_image))
            finally:
                if rgb_image is not pil_image:
                    rgb_image.close()
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

    def _ocr_image(
        self, image: bytes, *, page: int | None = None, media_id: str | None = None
    ) -> tuple[TextBlock, ...]:
        # 关闭 OCR 时直接短路返回空元组，避免误加载重量级 PaddleOCR 引擎。
        if not self._enable_ocr:
            return ()
        from PIL import Image

        with Image.open(io.BytesIO(image)) as decoded:
            return self._ocr_pil_image(decoded, page=page, media_id=media_id)

    def _pdf(self, content: bytes) -> ParsedDocument:
        import pypdfium2 as pdfium

        blocks: list[TextBlock] = []
        # 跨页图元去重缓存：键为 (宽, 高, blake2b-128)，值记录该图元 OCR 是否识别出文本。
        seen_image_hashes: dict[tuple[int, int, bytes], bool] = {}
        with _PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(content)
            try:
                if len(pdf) > 500:
                    raise ValueError("knowledge_pdf_page_limit")
                for number in range(len(pdf)):
                    page = pdf[number]
                    try:
                        textpage = page.get_textpage()
                        text = textpage.get_text_range().strip()
                        if text:
                            blocks.append(
                                TextBlock(text, ChunkLocator(kind="pdf", page=number + 1))
                            )
                        if self._enable_ocr:
                            seen_ocr: set[str] = set()
                            image_no = 0
                            image_ocr_succeeded = False
                            for image_object in page.get_objects():
                                if not hasattr(image_object, "get_bitmap"):
                                    continue
                                # 优先读图元像素元数据预过滤 < 32px 装饰图，避免触发位图光栅化。
                                get_px_size = getattr(image_object, "get_px_size", None)
                                if callable(get_px_size):
                                    try:
                                        px_width, px_height = get_px_size()
                                    except Exception:
                                        px_width, px_height = None, None
                                    if (
                                        isinstance(px_width, int)
                                        and isinstance(px_height, int)
                                        and (
                                            px_width < _MIN_PDF_OCR_IMAGE_DIMENSION
                                            or px_height < _MIN_PDF_OCR_IMAGE_DIMENSION
                                        )
                                    ):
                                        continue
                                bitmap = image_object.get_bitmap()
                                pil_image: Any = None
                                try:
                                    pil_image = bitmap.to_pil()
                                    # 兜底尺寸校验：过滤无 px_size 或位图小于阈值的装饰图元。
                                    if (
                                        pil_image.width < _MIN_PDF_OCR_IMAGE_DIMENSION
                                        or pil_image.height < _MIN_PDF_OCR_IMAGE_DIMENSION
                                    ):
                                        continue
                                    if pil_image.width * pil_image.height > 40_000_000:
                                        raise ValueError("knowledge_image_too_large")
                                    image_no += 1
                                    import numpy as np

                                    rgb_image = (
                                        pil_image
                                        if pil_image.mode == "RGB"
                                        else pil_image.convert("RGB")
                                    )
                                    try:
                                        pixels = np.ascontiguousarray(np.asarray(rgb_image))
                                    finally:
                                        if rgb_image is not pil_image:
                                            rgb_image.close()
                                    image_key = (
                                        pil_image.width,
                                        pil_image.height,
                                        hashlib.blake2b(
                                            memoryview(pixels), digest_size=16
                                        ).digest(),
                                    )
                                    cached_had_text = seen_image_hashes.get(image_key)
                                    if cached_had_text is not None:
                                        # 命中跨页去重缓存直接跳过 OCR，并同步回传有文本状态，
                                        # 防止稀疏正文页因跳过重复图元而误触发整页回退 OCR。
                                        image_ocr_succeeded = image_ocr_succeeded or cached_had_text
                                        continue
                                    ocr_blocks = self._ocr_pil_image(
                                        pil_image,
                                        page=number + 1,
                                        media_id=f"pdf-page-{number + 1}-image-{image_no}",
                                        _pixels=pixels,
                                    )
                                    had_text = bool(ocr_blocks)
                                    if len(seen_image_hashes) < _MAX_PDF_SEEN_IMAGE_HASHES:
                                        seen_image_hashes[image_key] = had_text
                                    image_ocr_succeeded = image_ocr_succeeded or had_text
                                    for block in ocr_blocks:
                                        normalized = " ".join(block.text.casefold().split())
                                        if (
                                            normalized
                                            and normalized not in seen_ocr
                                            and normalized not in text.casefold()
                                        ):
                                            blocks.append(block)
                                            seen_ocr.add(normalized)
                                finally:
                                    # 先关 PIL 视图再释放 PDFium 位图，防释放共享缓冲区后悬垂访问。
                                    if pil_image is not None and hasattr(pil_image, "close"):
                                        pil_image.close()
                                    if hasattr(bitmap, "close"):
                                        bitmap.close()
                            if len(text) < self._min_pdf_chars and not image_ocr_succeeded:
                                page_bitmap = page.render(scale=2)
                                page_pil: Any = None
                                try:
                                    page_pil = page_bitmap.to_pil()
                                    blocks.extend(
                                        self._ocr_pil_image(
                                            page_pil,
                                            page=number + 1,
                                            media_id=f"pdf-page-{number + 1}",
                                        )
                                    )
                                finally:
                                    if page_pil is not None and hasattr(page_pil, "close"):
                                        page_pil.close()
                                    if hasattr(page_bitmap, "close"):
                                        page_bitmap.close()
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
        if self._enable_ocr:
            from PIL import UnidentifiedImageError

            seen: set[str] = set()
            for rel in doc.part.rels.values():
                # 外链图片没有内嵌 target_part，跳过以避免解析抛错或产生外网依赖。
                if getattr(rel, "is_external", False):
                    continue
                if "image" in rel.reltype and rel.target_ref not in seen:
                    seen.add(rel.target_ref)
                    try:
                        blocks.extend(
                            self._ocr_image(rel.target_part.blob, media_id=rel.target_ref)
                        )
                    except (UnidentifiedImageError, OSError) as error:
                        # 跳过 EMF/WMF/SVG 等 PIL 不支持的矢量图或单张损坏图，不阻断整篇文档导入。
                        logger.warning(
                            "docx_unsupported_image_skipped media_id=%s exception_type=%s",
                            rel.target_ref,
                            type(error).__name__,
                        )
                        continue
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
        if self._enable_ocr:
            # 先低成本扫描 ZIP 目录判断是否存在嵌入式图片或绘图；
            # 纯表格工作簿直接跳过 read_only=False 二次全量 DOM 加载以大幅降低内存峰值。
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                has_embedded_media = any(
                    name.lower().startswith(("xl/media/", "xl/drawings/"))
                    for name in archive.namelist()
                )
            if has_embedded_media:
                image_workbook = load_workbook(io.BytesIO(content), read_only=False, data_only=True)
                try:
                    for sheet in image_workbook.worksheets:
                        for image_no, image in enumerate(getattr(sheet, "_images", ()), 1):
                            # 兼容 AbsoluteAnchor 等不含 _from 单元格坐标的锚点，避免属性缺失崩溃。
                            anchor_from = getattr(getattr(image, "anchor", None), "_from", None)
                            if (
                                anchor_from is not None
                                and hasattr(anchor_from, "col")
                                and hasattr(anchor_from, "row")
                            ):
                                location = f"cell-{anchor_from.col + 1}:{anchor_from.row + 1}"
                            else:
                                location = f"pos-{image_no}"
                            blocks.extend(
                                self._ocr_image(
                                    image._data(),
                                    media_id=f"sheet-{sheet.title}-image-{image_no}-{location}",
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
            if self._enable_ocr:
                blocks.extend(
                    self._ocr_image(shape.image.blob, media_id=f"slide-{slide_no}-shape-{path}")
                )
        elif shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            for index, child in enumerate(shape.shapes, 1):
                blocks.extend(self._pptx_shape(child, slide_no, f"{path}.{index}", shape_types))
        return blocks


class LocalDocumentParserFactory(DocumentParserFactory):
    """在解析子进程中创建本地解析器，避免父进程加载 OCR runtime。"""

    def __init__(self, *, ocr_languages: str = DEFAULT_OCR_LANGUAGES) -> None:
        self._ocr_languages = ocr_languages

    def create(self) -> DocumentParser:
        return LocalDocumentParser(ocr_languages=self._ocr_languages)
