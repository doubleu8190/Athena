"""PDF 文件适配器。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from athena.config.settings import Settings
from athena.core.files.base import ExtractedUnit, ExtractionContext, ExtractionResult
from athena.models.file import AdapterInfo

from .common import _pil_image_png_bytes, _rapidocr_text, logger


class PdfAdapter:
    """PDF 文档适配器，支持文本、OCR 和表格提取。"""

    info = AdapterInfo(
        name="pdf",
        version="2.0",
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
            content = (page.extract_text() or "").strip()
            if not content:
                empty_pages.append(index)
                content = self._ocr_page(path, index - 1)
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
