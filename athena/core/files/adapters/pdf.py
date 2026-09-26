"""PDF 文件适配器。"""

from __future__ import annotations

import asyncio
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


# The HanyuXi font embedded by several Chinese textbooks stores tone-marked
# vowels in ASCII slots.  pypdf therefore returns values such as ``xiAo`` for
# ``xiǎo``.  Keep the table local to the PDF adapter because it describes a
# source-PDF encoding, rather than a general text normalization rule.
_HANYUXI_PINYIN = {
    "A": "ǎ",
    "C": "ǜ",
    "D": "ě",
    "E": "ē",
    "F": "è",
    "G": "ǒ",
    "H": "ò",
    "I": "í",
    "J": "ǐ",
    "K": "ì",
    "L": "ǔ",
    "M": "ù",
    "N": "ū",
    "O": "ū",
    "P": "ú",
    "Q": "ā",
    "R": "é",
    "S": "à",
    "T": "ō",
    "U": "ī",
    "V": "ǘ",
    "W": "á",
    "Y": "ó",
}
_PINYIN_TOKEN = re.compile(r"(?<![A-Za-z])[A-Za-z]*[A-Z][A-Za-z]*(?![A-Za-z])")


def _clean_pdf_text(text: str) -> str:
    """Remove hidden PDF trailer data and decode textbook pinyin glyphs."""
    # Some textbook PDFs append a hidden printing string containing C0/C1
    # controls to every page.  It starts on its own line (or after a newline),
    # so discard that line and everything after the first forbidden control.
    controls = re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", text)
    if controls:
        text = text[: text.rfind("\n", 0, controls.start()) + 1]

    def decode(match: re.Match[str]) -> str:
        token = match.group(0)
        # Headings such as YUWEN and ordinary Latin words are not HanyuXi
        # pinyin tokens.  A lowercase consonant/vowel context is required.
        if not any(char.islower() for char in token):
            if len(token) == 1 and token in _HANYUXI_PINYIN:
                return _HANYUXI_PINYIN[token]
            return token
        return "".join(_HANYUXI_PINYIN.get(char, char) for char in token)

    text = _PINYIN_TOKEN.sub(decode, text)
    # Keep output stable for token counting and remove other invisible format
    # controls without touching ordinary whitespace used by PDF layout.
    return "".join(
        char
        for char in unicodedata.normalize("NFC", text)
        if char in "\n\r\t" or not unicodedata.category(char).startswith("C")
    ).strip()


class PdfAdapter:
    """PDF 文档适配器，支持文本、OCR 和表格提取。"""

    info = AdapterInfo(
        name="pdf",
        version="2.1",
        mime_types=["application/pdf"],
        extensions=[".pdf"],
        capabilities=["read", "search", "summarize", "analyze", "extract_table"],
    )

    async def extract(
        self, context: ExtractionContext, settings: Settings
    ) -> ExtractionResult:
        """提取 PDF 内容。"""
        return await asyncio.to_thread(self._extract, context.path)

    def _extract(self, path: Path) -> ExtractionResult:
        """同步提取 PDF 文本、OCR 和表格数据。"""
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        units: list[ExtractedUnit] = []
        empty_pages: list[int] = []
        for index, page in enumerate(reader.pages, 1):
            content = _clean_pdf_text(page.extract_text() or "")
            if not content:
                empty_pages.append(index)
                content = _clean_pdf_text(self._ocr_page(path, index - 1))
            if content:
                units.append(
                    ExtractedUnit(
                        content,
                        {"page": index},
                        {"kind": "page", "ocr": index in empty_pages},
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
                "table_count": len(tables),
            },
            tables=tables,
        )

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
