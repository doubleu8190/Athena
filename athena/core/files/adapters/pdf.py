"""PDF 文件适配器。"""

from __future__ import annotations

import asyncio
import io
import re
import unicodedata
from pathlib import Path
from typing import Any

from athena.config.settings import Settings
from athena.core.files.extraction import (
    ExtractedUnit,
    ExtractionContext,
    ExtractionResult,
)
from athena.models.file import AdapterInfo

from .common import _pil_image_png_bytes, _rapidocr_text, logger

_PAGE_NUMBER = re.compile(r"^\s*\d+\s*$")
_SECTION_START = re.compile(r"^(?:chapter|section|part|contents?)\b", re.IGNORECASE)
_NON_PROSE_PAGE = re.compile(
    r"^(?:contents?|table of contents|copyright|isbn|目录|版权|定价|出版)\b",
    re.IGNORECASE,
)


def _clean_pdf_text(text: str) -> str:
    """Remove hidden PDF trailer data and normalize invisible characters."""
    # Some PDFs append hidden printing data containing C0/C1 controls to every
    # page. It starts on its own line, so discard it and everything after it.
    controls = re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", text)
    if controls:
        text = text[: text.rfind("\n", 0, controls.start()) + 1]

    # Keep output stable for token counting and remove other invisible format
    # controls without touching ordinary whitespace used by PDF layout.
    return "".join(
        char
        for char in unicodedata.normalize("NFC", text)
        if char in "\n\r\t" or not unicodedata.category(char).startswith("C")
    ).strip()


def _page_body_lines(content: str) -> list[str]:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    while lines and _PAGE_NUMBER.fullmatch(lines[-1]):
        lines.pop()
    # Running headers and section/page labels are outside the body.
    while lines and _SECTION_START.match(lines[-1]):
        lines.pop()
    return lines


def _is_structural_page(lines: list[str]) -> bool:
    """Detect front matter and navigation pages from their text shape.

    This deliberately uses page content rather than page numbers or a known
    document layout.  It is only a conservative merge barrier; normal prose
    is still allowed to continue across a page boundary.
    """
    if not lines:
        return True
    first = lines[0]
    if _NON_PROSE_PAGE.match(first):
        return True
    if len(lines) >= 3 and sum(len(line) <= 12 for line in lines) >= len(lines) - 1:
        return True
    visible = "".join(lines)
    sentence_marks = len(re.findall(r"[。！？.!?]", visible))
    long_lines = sum(len(line) >= 24 for line in lines)
    digit_lines = sum(bool(re.search(r"\d", line)) for line in lines)
    cjk_chars = len(re.findall(r"[\u3400-\u9fff]", visible))
    return (
        2 <= len(lines) <= 8
        and long_lines == 0
        and sentence_marks == 0
        and cjk_chars == 0
    ) or (digit_lines >= max(3, len(lines) // 3) and sentence_marks == 0)


def _page_can_merge_before(content: str) -> bool:
    lines = _page_body_lines(content)
    if not lines or _is_structural_page(lines) or _SECTION_START.match(lines[0]):
        return False
    return True


def _page_can_merge_after(content: str) -> bool:
    lines = _page_body_lines(content)
    if not lines or _is_structural_page(lines):
        return False
    last = lines[-1]
    return not bool(re.search(r"[。！？；!?;：:](?:[”’'）)]*)$", last))


class PdfAdapter:
    """PDF 文档适配器，支持文本、OCR 和表格提取。"""

    info = AdapterInfo(
        name="pdf",
        version="3.1",
        mime_types=["application/pdf"],
        extensions=[".pdf"],
        capabilities=["read", "search", "summarize", "analyze", "extract_table"],
    )

    async def extract(
        self, context: ExtractionContext, settings: Settings
    ) -> ExtractionResult:
        """提取 PDF 内容。"""
        return await asyncio.to_thread(self._extract, context.path, settings)

    def _extract(
        self, path: Path, settings: Settings | None = None
    ) -> ExtractionResult:
        """同步提取 PDF 文本、OCR 和表格数据。"""
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        units: list[ExtractedUnit] = []
        empty_pages: list[int] = []
        image_ocr_pages: list[int] = []
        full_page_ocr_pages: list[int] = []
        embedded_image_count = 0
        ocr_image_count = 0
        ocr_mixed_pages = bool(
            getattr(settings, "pdf_ocr_mixed_pages", True)
        )
        min_image_pixels = int(
            getattr(settings, "pdf_ocr_min_image_pixels", 4096)
        )
        max_images_per_page = int(
            getattr(settings, "pdf_ocr_max_images_per_page", 20)
        )
        for index, page in enumerate(reader.pages, 1):
            native_content = _clean_pdf_text(page.extract_text() or "")
            embedded_images = self._page_images(page)
            embedded_image_count += len(embedded_images)
            image_ocr: list[str] = []
            if ocr_mixed_pages and embedded_images:
                for image in embedded_images[:max_images_per_page]:
                    image_text = self._ocr_embedded_image(
                        image, min_image_pixels=min_image_pixels
                    )
                    if image_text:
                        image_ocr.append(image_text)
                ocr_image_count += len(image_ocr)
                if image_ocr:
                    image_ocr_pages.append(index)

            # A full-page OCR fallback is only needed when native extraction and
            # embedded-image OCR both produced no usable text. This preserves
            # native text on mixed pages and avoids indexing it twice.
            full_page_ocr = ""
            if not native_content and not image_ocr:
                empty_pages.append(index)
                full_page_ocr = _clean_pdf_text(self._ocr_page(path, index - 1))
                if full_page_ocr:
                    full_page_ocr_pages.append(index)

            content = self._merge_page_text(
                native_content,
                [*image_ocr, full_page_ocr] if full_page_ocr else image_ocr,
            )
            if content:
                ocr_sources = []
                if image_ocr:
                    ocr_sources.append("embedded_image")
                if full_page_ocr:
                    ocr_sources.append("full_page")
                units.append(
                    ExtractedUnit(
                        content,
                        {"page": index},
                        {
                            "kind": "page",
                            "ocr": bool(ocr_sources),
                            "ocr_source": "+".join(ocr_sources) or None,
                            "image_count": len(embedded_images),
                        },
                        kind="page",
                        block_id=f"page-{index:04d}",
                        can_merge_before=_page_can_merge_before(content),
                        can_merge_after=_page_can_merge_after(content),
                    )
                )
        tables: list[dict[str, Any]] = []
        try:
            import pdfplumber

            with pdfplumber.open(path) as pdf:
                for page_no, page in enumerate(pdf.pages, 1):
                    for table_no, table in enumerate(page.extract_tables(), 1):
                        tables.append(
                            {"page": page_no, "table": table_no, "rows": table}
                        )
        except Exception as exc:
            logger.warning("pdf_table_extract_failed", path=str(path), error=str(exc))
        return ExtractionResult(
            units=units,
            metadata={
                "pages": len(reader.pages),
                "ocr_pages": empty_pages,
                "full_page_ocr_pages": full_page_ocr_pages,
                "image_ocr_pages": image_ocr_pages,
                "embedded_image_count": embedded_image_count,
                "ocr_image_count": ocr_image_count,
                "table_count": len(tables),
            },
            tables=tables,
        )

    @staticmethod
    def _page_images(page: Any) -> list[Any]:
        """读取页面内嵌图片，遇到不支持的 PDF image object 时降级为空。"""
        try:
            return list(page.images)
        except Exception as exc:
            logger.warning("pdf_image_enumeration_failed", error=str(exc))
            return []

    @staticmethod
    def _ocr_embedded_image(image: Any, *, min_image_pixels: int) -> str:
        """对单个内嵌图片执行 OCR，并过滤过小的装饰图片。"""
        data = getattr(image, "data", b"")
        if not data:
            return ""
        if min_image_pixels > 0:
            try:
                from PIL import Image

                with Image.open(io.BytesIO(data)) as decoded:
                    if decoded.width * decoded.height < min_image_pixels:
                        return ""
            except Exception:
                # OCR can still understand formats Pillow cannot inspect; let
                # RapidOCR decide whether the raw image bytes are usable.
                pass
        try:
            return _clean_pdf_text(_rapidocr_text(data))
        except Exception as exc:
            logger.warning("pdf_embedded_image_ocr_failed", error=str(exc))
            return ""

    @staticmethod
    def _merge_page_text(native_text: str, ocr_texts: list[str]) -> str:
        """合并原生文本和 OCR 文本，避免完全重复的识别结果。"""
        parts = [native_text] if native_text else []
        normalized_parts = {
            re.sub(r"\s+", " ", native_text).strip().casefold()
        } if native_text else set()
        for text in ocr_texts:
            cleaned = _clean_pdf_text(text)
            if not cleaned:
                continue
            normalized = re.sub(r"\s+", " ", cleaned).strip().casefold()
            if not normalized or normalized in normalized_parts:
                continue
            # Some PDF producers expose the same image text as native text.
            # Skip an OCR fragment only when it is wholly contained in the
            # native text; unrelated image text must still be retained.
            if native_text and normalized in re.sub(
                r"\s+", " ", native_text
            ).strip().casefold():
                continue
            parts.append(cleaned)
            normalized_parts.add(normalized)
        return "\n\n".join(parts).strip()

    @staticmethod
    def _ocr_page(path: Path, page_index: int) -> str:
        """对单页 PDF 执行 OCR，失败时返回空字符串。"""
        try:
            import pypdfium2 as pdfium

            document = pdfium.PdfDocument(str(path))
            image = document[page_index].render(scale=2).to_pil()
            return _rapidocr_text(_pil_image_png_bytes(image))
        except Exception as exc:
            logger.warning(
                "pdf_ocr_failed", path=str(path), page_index=page_index, error=str(exc)
            )
            return ""

    async def analyze(self, path: Path, task: str) -> dict[str, Any]:
        """分析 PDF 并返回文件元数据。"""
        result = await asyncio.to_thread(self._extract, path)
        return result.metadata
