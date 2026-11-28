"""Local parsers for supported knowledge source formats."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import threading
import zipfile
from collections import Counter
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
# 目录引导点线（连续 4 个及以上 . / · / … 含可选空格），避开小数、省略号 ... 与代码片段。
_TOC_DOT_LEADER_RE = re.compile(r"[ \t]*(?:[.·…][ \t]*){4,}")
_EXCESS_NEWLINES_RE = re.compile(r"\n{3,}")
_DIGITS_RE = re.compile(r"\d+")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+)?\s*$")
# 按 CommonMark 4.5 区分围栏起始：反引号围栏 info string 不允许含反引号，防止行内代码误开围栏。
_MD_FENCE_OPEN_RE = re.compile(r"^\s*(?:(`{3,})[^`]*|(~{3,}).*)$")

logger = logging.getLogger(__name__)


def _clean_toc_dot_leaders(text: str) -> str:
    """将目录引导点线压缩为单个空格，同时保留正常小数、三点省略号及普通标点。"""
    return _TOC_DOT_LEADER_RE.sub(" ", text)


def _clean_extracted_text(text: str) -> str:
    """轻量预清洗提取文本：统一换行符、压缩目录引导点线并折叠多余空行。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _clean_toc_dot_leaders(normalized)
    normalized = _EXCESS_NEWLINES_RE.sub("\n\n", normalized)
    return normalized.strip()


def _normalize_header_footer_line(line: str) -> str | None:
    """归一化候选页眉/页脚行：忽略空行或超过 80 字符的正文行，将连续数字归一为 #。"""
    stripped = line.strip()
    if not stripped or len(stripped) > 80:
        return None
    return _DIGITS_RE.sub("#", stripped)


def _strip_repeated_pdf_headers_footers(page_texts: list[str]) -> list[str]:
    """跨页识别并剔除 PDF 重复页眉/页脚，减少检索噪声与重复 token 占用。"""
    page_records: list[tuple[list[str], int | None, int | None, str | None, str | None]] = []
    header_counts: Counter[str] = Counter()
    footer_counts: Counter[str] = Counter()
    non_empty_pages = 0

    for text in page_texts:
        lines = text.split("\n")
        first_idx = next((idx for idx, line in enumerate(lines) if line.strip()), None)
        if first_idx is None:
            page_records.append((lines, None, None, None, None))
            continue
        last_idx = next(idx for idx in range(len(lines) - 1, -1, -1) if lines[idx].strip())
        non_empty_pages += 1
        first_norm = _normalize_header_footer_line(lines[first_idx])
        if first_norm is not None:
            header_counts[first_norm] += 1
        # 单行页仅存在 1 行非空文本，不参与页脚统计，避免误删短页正文。
        last_norm = _normalize_header_footer_line(lines[last_idx]) if last_idx > first_idx else None
        if last_norm is not None:
            footer_counts[last_norm] += 1
        page_records.append((lines, first_idx, last_idx, first_norm, last_norm))

    # 非空页数不足 3 页时不启用，防止短文档正文首末行被误判为页眉/页脚。
    if non_empty_pages < 3:
        return list(page_texts)

    # 在已满足 non_empty_pages >= 3 的前提下，占比 >= 60% 即视为跨页重复页眉/页脚
    # （例如 3 页文档封面无页眉页脚、后 2 页重复占 66.7% 也可正确剔除）。
    repeated_headers = {
        norm for norm, count in header_counts.items() if (count / non_empty_pages) >= 0.6
    }
    repeated_footers = {
        norm for norm, count in footer_counts.items() if (count / non_empty_pages) >= 0.6
    }
    if not repeated_headers and not repeated_footers:
        return list(page_texts)

    cleaned_pages: list[str] = []
    for lines, first_line_idx, last_line_idx, first_norm, last_norm in page_records:
        if first_line_idx is None or last_line_idx is None:
            cleaned_pages.append("")
            continue
        drop_indices: set[int] = set()
        if first_norm in repeated_headers:
            drop_indices.add(first_line_idx)
        if last_line_idx > first_line_idx and last_norm in repeated_footers:
            drop_indices.add(last_line_idx)
        kept = [line for idx, line in enumerate(lines) if idx not in drop_indices]
        cleaned_pages.append("\n".join(kept).strip())
    return cleaned_pages


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
            if suffix == ".md":
                return self._markdown(text)
            blocks: list[TextBlock] = []
            for number, line in enumerate(text.splitlines(), 1):
                cleaned_line = _clean_toc_dot_leaders(line).rstrip()
                if cleaned_line.strip():
                    blocks.append(
                        TextBlock(
                            cleaned_line,
                            ChunkLocator(kind="text", start_line=number, end_line=number),
                        )
                    )
            return ParsedDocument(tuple(blocks))
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

    def _markdown(self, text: str) -> ParsedDocument:
        """按 Markdown 结构解析：维护 ATX 多级标题栈，并保持围栏代码块整体不拆分。"""
        lines = text.splitlines()
        blocks: list[TextBlock] = []
        heading_stack: list[tuple[int, str]] = []
        current_heading: str | None = None

        in_fence = False
        fence_char = ""
        fence_len = 0
        fence_start_line = 0
        fence_lines: list[str] = []

        for number, line in enumerate(lines, 1):
            if in_fence:
                fence_lines.append(line)
                stripped = line.strip()
                # 匹配相同围栏字符且长度不少于起始围栏的闭合行，结束代码块收集。
                if len(stripped) >= fence_len and set(stripped) == {fence_char}:
                    blocks.append(
                        TextBlock(
                            "\n".join(fence_lines),
                            ChunkLocator(
                                kind="markdown",
                                start_line=fence_start_line,
                                end_line=number,
                                heading=current_heading,
                            ),
                            kind="code",
                        )
                    )
                    in_fence = False
                    fence_lines = []
                continue

            fence_match = _MD_FENCE_OPEN_RE.match(line)
            if fence_match is not None:
                marker = fence_match.group(1) or fence_match.group(2)
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
                fence_start_line = number
                fence_lines = [line]
                continue

            heading_match = _MD_HEADING_RE.match(line)
            if heading_match is not None:
                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()
                # 遇到同级或更高级标题时回退标题栈，保持面包屑路径准确。
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, title))
                current_heading = " > ".join(item_title for _, item_title in heading_stack)
                blocks.append(
                    TextBlock(
                        line.rstrip(),
                        ChunkLocator(
                            kind="markdown",
                            start_line=number,
                            end_line=number,
                            heading=current_heading,
                        ),
                        kind="text",
                    )
                )
                continue

            cleaned_line = _clean_toc_dot_leaders(line).rstrip()
            if cleaned_line.strip():
                blocks.append(
                    TextBlock(
                        cleaned_line,
                        ChunkLocator(
                            kind="markdown",
                            start_line=number,
                            end_line=number,
                            heading=current_heading,
                        ),
                        kind="text",
                    )
                )

        # 文档末尾存在未闭合围栏时，仍将已收集代码作为完整代码块输出。
        if in_fence and fence_lines:
            blocks.append(
                TextBlock(
                    "\n".join(fence_lines),
                    ChunkLocator(
                        kind="markdown",
                        start_line=fence_start_line,
                        end_line=len(lines),
                        heading=current_heading,
                    ),
                    kind="code",
                )
            )
        return ParsedDocument(tuple(blocks))

    def _pdf(self, content: bytes) -> ParsedDocument:
        import pypdfium2 as pdfium

        blocks: list[TextBlock] = []
        # 跨页图元去重缓存：键为 (宽, 高, blake2b-128)，值记录该图元 OCR 是否识别出文本。
        seen_image_hashes: dict[tuple[int, int, bytes], bool] = {}
        with _PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(content)
            try:
                page_count = len(pdf)
                if page_count > 500:
                    raise ValueError("knowledge_pdf_page_limit")
                # 第一遍收集全页清洗后的原生文本，以便跨页识别并剔除重复页眉/页脚。
                raw_page_texts: list[str] = []
                for number in range(page_count):
                    page = pdf[number]
                    try:
                        textpage = page.get_textpage()
                        raw_page_texts.append(_clean_extracted_text(textpage.get_text_range()))
                    finally:
                        page.close()
                cleaned_page_texts = _strip_repeated_pdf_headers_footers(raw_page_texts)

                # 第二遍按页输出清洗文本块，并在开启 OCR 时处理嵌入图元与稀疏页整页回退。
                for number, text in enumerate(cleaned_page_texts):
                    raw_text = raw_page_texts[number]
                    raw_text_casefold = raw_text.casefold()
                    if text:
                        blocks.append(TextBlock(text, ChunkLocator(kind="pdf", page=number + 1)))
                    if not self._enable_ocr:
                        continue
                    page = pdf[number]
                    try:
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
                                    hashlib.blake2b(memoryview(pixels), digest_size=16).digest(),
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
                                        and normalized not in raw_text_casefold
                                    ):
                                        blocks.append(block)
                                        seen_ocr.add(normalized)
                            finally:
                                # 先关 PIL 视图再释放 PDFium 位图，防释放共享缓冲区后悬垂访问。
                                if pil_image is not None and hasattr(pil_image, "close"):
                                    pil_image.close()
                                if hasattr(bitmap, "close"):
                                    bitmap.close()
                        # 仅缺少原生可选文本的扫描页才触发整页 OCR，避免原生短正文页剔除页眉/页脚后
                        # 误触发 OCR（防无 OCR 环境失败，也防已剔除页眉/页脚被重新识别入库）。
                        if len(raw_text) < self._min_pdf_chars and not image_ocr_succeeded:
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
