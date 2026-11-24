"""会话附件 PDF / Office 安全文档文本提取器。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

from domain.file import (
    EXTRACTABLE_DOCUMENT_EXTENSIONS,
    MAX_SESSION_DOCUMENT_EXTRACT_BYTES,
    MAX_SESSION_EXTRACTED_TEXT_BYTES,
    SessionDocumentExtractionResult,
    SessionDocumentExtractor,
)
from domain.knowledge.model import TextBlock
from infrastructure.knowledge.parsers import LocalDocumentParser

logger = logging.getLogger(__name__)

# 会话附件解析默认超时（秒），避免复杂/恶意文件长期占满工作线程。
DEFAULT_DOCUMENT_EXTRACT_TIMEOUT_SECONDS = 15.0

_VALIDATION_ERROR_MAP: dict[str, str] = {
    "knowledge_file_size_invalid": "document_extract_too_large",
    "knowledge_archive_encrypted": "document_encrypted",
    "knowledge_office_macro_unsupported": "document_macro_unsupported",
    "knowledge_pdf_page_limit": "document_page_limit_exceeded",
    "knowledge_archive_member_limit": "document_archive_limit_exceeded",
    "knowledge_archive_expanded_size_limit": "document_archive_limit_exceeded",
    "knowledge_extracted_text_limit": "document_extracted_text_too_large",
    "knowledge_spreadsheet_cell_limit": "document_extracted_text_too_large",
    "knowledge_document_text_empty": "document_no_extractable_text",
    "knowledge_file_type_unsupported": "document_type_unsupported",
    "knowledge_legacy_office_unsupported": "document_type_unsupported",
}


class LocalSessionDocumentExtractor(SessionDocumentExtractor):
    """复用 LocalDocumentParser(enable_ocr=False) 提取 PDF / Office 结构化纯文本。"""

    def __init__(
        self,
        *,
        parser: LocalDocumentParser | None = None,
        max_file_bytes: int = MAX_SESSION_DOCUMENT_EXTRACT_BYTES,
        max_text_bytes: int = MAX_SESSION_EXTRACTED_TEXT_BYTES,
        timeout_seconds: float = DEFAULT_DOCUMENT_EXTRACT_TIMEOUT_SECONDS,
    ) -> None:
        """保存无 OCR 解析器实例及单文件、提取文本与超时边界。"""
        if max_file_bytes < 1:
            raise ValueError("document_extract_max_file_bytes_invalid")
        if max_text_bytes < 1:
            raise ValueError("document_extract_max_text_bytes_invalid")
        if timeout_seconds <= 0:
            raise ValueError("document_extract_timeout_invalid")
        self._parser = parser if parser is not None else LocalDocumentParser(enable_ocr=False)
        self._max_file_bytes = max_file_bytes
        self._max_text_bytes = max_text_bytes
        self._timeout_seconds = timeout_seconds

    async def extract(
        self,
        filename: str,
        path: Path,
    ) -> SessionDocumentExtractionResult:
        """在有界大小、超时和线程边界内提取 UTF-8 结构化文本，不泄露原始异常细节。"""
        suffix = Path(filename).suffix.lower()
        if suffix not in EXTRACTABLE_DOCUMENT_EXTENSIONS:
            return SessionDocumentExtractionResult.failed("document_type_unsupported")
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await asyncio.to_thread(self._extract_sync, filename, suffix, path)
        except TimeoutError:
            logger.warning(
                "session_document_extract_timeout suffix=%s timeout_seconds=%s",
                suffix,
                self._timeout_seconds,
            )
            return SessionDocumentExtractionResult.failed("document_extract_timeout")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = _map_extraction_error(error)
            logger.warning(
                "session_document_extract_failed suffix=%s code=%s exception_type=%s",
                suffix,
                code,
                type(error).__name__,
            )
            return SessionDocumentExtractionResult.failed(code)

    def _extract_sync(
        self,
        filename: str,
        suffix: str,
        path: Path,
    ) -> SessionDocumentExtractionResult:
        """在阻塞工作线程内依次执行文件大小拦截、安全校验、解析与结构化拼接。"""
        if not path.is_file():
            return SessionDocumentExtractionResult.failed("document_parse_failed")
        if path.stat().st_size > self._max_file_bytes:
            return SessionDocumentExtractionResult.failed("document_extract_too_large")
        # 复用知识库解析器的安全防线：PDF 500 页上限、Office ZIP 炸弹/加密/宏拦截。
        self._parser.validate_file(filename, path, max_bytes=self._max_file_bytes)
        content = path.read_bytes()
        if len(content) > self._max_file_bytes:
            return SessionDocumentExtractionResult.failed("document_extract_too_large")
        parsed = self._parser.parse_sync(filename, content)
        rendered = render_parsed_document_text(suffix, parsed.blocks)
        if not rendered:
            return SessionDocumentExtractionResult.failed("document_no_extractable_text")
        # 严格校验提取后 UTF-8 字节数不超过 10 MiB，防止极端压缩比文档写爆对象缓存。
        if len(rendered.encode("utf-8")) > self._max_text_bytes:
            return SessionDocumentExtractionResult.failed("document_extracted_text_too_large")
        return SessionDocumentExtractionResult.ready(rendered)


def render_parsed_document_text(suffix: str, blocks: Sequence[TextBlock]) -> str:
    """按定位器变化插入简洁 Markdown 分节标头，将解析块拼接为高可读 UTF-8 文本。"""
    sections: list[list[str]] = []
    current_section_key: tuple[str, object] | None = None
    current_lines: list[str] = []

    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        section_key, header = _section_header_for_block(suffix, block)
        if header is not None:
            if section_key != current_section_key:
                if current_lines:
                    sections.append(current_lines)
                current_lines = [header]
                current_section_key = section_key
        elif suffix == ".docx":
            # DOCX 普通正文不输出 "## Normal"，但重置标题游标，确保后续同级标题仍生成分节标头。
            current_section_key = None
        current_lines.append(text)

    if current_lines:
        sections.append(current_lines)
    return "\n\n".join("\n".join(lines) for lines in sections).strip()


def _section_header_for_block(
    suffix: str,
    block: TextBlock,
) -> tuple[tuple[str, object] | None, str | None]:
    """根据文档格式与块定位器生成稳定的分节键与分节标头。"""
    locator = block.locator
    if suffix == ".pdf" and locator.page is not None:
        return ("pdf", locator.page), f"## 第 {locator.page} 页"
    if suffix == ".xlsx" and locator.sheet:
        sheet_name = " ".join(locator.sheet.split())
        if sheet_name:
            return ("xlsx", sheet_name), f"## 工作表：{sheet_name}"
    if suffix == ".pptx" and locator.slide is not None:
        return ("pptx", locator.slide), f"## 幻灯片 {locator.slide}"
    if suffix == ".docx" and locator.heading:
        heading = " ".join(locator.heading.split())
        if heading and heading.casefold() != "normal":
            return ("docx", heading), f"## {heading}"
    return None, None


def _map_extraction_error(error: Exception) -> str:
    """将底层解析或校验异常统一映射为稳定、可安全公开的错误码。"""
    if isinstance(error, ValueError):
        raw_code = str(error)
        if raw_code == "knowledge_pdf_invalid":
            if _is_pdf_password_error(error.__cause__):
                return "document_encrypted"
            return "document_parse_failed"
        return _VALIDATION_ERROR_MAP.get(raw_code, "document_parse_failed")
    if _is_pdf_password_error(error):
        return "document_encrypted"
    return "document_parse_failed"


def _is_pdf_password_error(error: BaseException | None) -> bool:
    """识别 pypdfium2 密码保护 PDF 错误（FPDF_ERR_PASSWORD == 4）。"""
    if error is None:
        return False
    if getattr(error, "err_code", None) == 4:
        return True
    return "password" in str(error).casefold()


__all__ = [
    "DEFAULT_DOCUMENT_EXTRACT_TIMEOUT_SECONDS",
    "LocalSessionDocumentExtractor",
    "render_parsed_document_text",
]
