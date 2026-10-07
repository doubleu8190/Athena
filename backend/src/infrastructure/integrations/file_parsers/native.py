"""Small target-owned file ingestion primitives.

The parser deliberately has no dependency on the former ``core.files``
package.  Rich format-specific parsers can implement the same port and be
selected by the bootstrap registry later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from io import BytesIO
import csv
import hashlib
import json
from uuid import uuid4

from domain.files import (
    AdapterInfo,
    Attachment,
    FileChunk,
    FileLocator,
    FileMetadata,
    ParsedDocument,
)


class NativeFileAdapterRegistry:
    """Content-type registry used by the production target graph."""

    _adapters = (
        AdapterInfo("text", "1", ("text/plain", "text/markdown", "application/json", "text/csv"), (".txt", ".md", ".json", ".csv", ".log", ".py", ".js", ".ts", ".yaml", ".yml"), ("text",)),
        AdapterInfo("pdf", "1", ("application/pdf",), (".pdf",), ("text", "pages")),
        AdapterInfo("word", "1", ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",), (".docx",), ("text", "paragraphs")),
        AdapterInfo("excel", "1", ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/csv"), (".xlsx", ".xls", ".csv"), ("text", "tables")),
        AdapterInfo("image", ("1"), ("image/png", "image/jpeg", "image/webp", "image/gif"), (".png", ".jpg", ".jpeg", ".webp", ".gif"), ("binary",)),
    )

    def select(self, filename: str, mime_type: str) -> AdapterInfo:
        value = filename.lower()
        for adapter in self._adapters:
            if mime_type in adapter.mime_types or any(value.endswith(extension) for extension in adapter.extensions):
                return adapter
        if not value:
            raise ValueError(f"unsupported file type: {filename}")
        raise ValueError(f"unsupported file type: {filename}")

    def supported_extensions(self) -> list[str]:
        return [extension for adapter in self._adapters for extension in adapter.extensions]


class NativeTextParser:
    """Parse common text, PDF, Word, Excel and image attachments."""

    def __init__(self, storage) -> None:
        self._storage = storage

    async def parse(self, attachment: Attachment) -> ParsedDocument:
        data = await self._storage.read(attachment.storage_key)
        filename = getattr(attachment, "filename", "")
        suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        metadata: dict[str, object] = {"encoding": "utf-8"}
        if suffix == "pdf":
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(data))
            pages = [(page.extract_text() or "") for page in reader.pages]
            text = "\n\n".join(pages)
            metadata = {"pages": len(pages), "format": "pdf"}
        elif suffix == "docx":
            from docx import Document
            document = Document(BytesIO(data))
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
            metadata = {"paragraphs": len(document.paragraphs), "format": "docx"}
        elif suffix in {"xlsx", "xls"}:
            from openpyxl import load_workbook
            workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
            lines: list[str] = []
            for sheet in workbook.worksheets:
                lines.append(f"# {sheet.title}")
                for row in sheet.iter_rows(values_only=True):
                    lines.append("\t".join("" if value is None else str(value) for value in row))
            text = "\n".join(lines)
            metadata = {"sheets": len(workbook.worksheets), "format": "xlsx"}
        elif suffix == "csv":
            decoded = data.decode("utf-8", errors="replace")
            rows = list(csv.reader(decoded.splitlines()))
            text = "\n".join("\t".join(row) for row in rows)
            metadata = {"rows": len(rows), "format": "csv"}
        elif suffix in {"png", "jpg", "jpeg", "webp", "gif"}:
            text = f"[image attachment: {filename or 'unknown'}]"
            metadata = {"format": suffix, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        else:
            text = data.decode("utf-8", errors="replace")
            if suffix == "json":
                try:
                    text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
                except (TypeError, ValueError):
                    pass
        return ParsedDocument(
            text=text,
            metadata=FileMetadata(metadata),
            adapter_name=attachment.adapter_name or "text",
            adapter_version=attachment.adapter_version or "1",
            capabilities=attachment.capabilities or ("text",),
        )


def line_chunker(*, max_characters: int = 4000) -> Callable[[str], list[FileChunk]]:
    """Create deterministic chunks without importing a tokenizer SDK."""

    if max_characters < 1:
        raise ValueError("max_characters must be positive")

    def chunk(text: str) -> list[FileChunk]:
        values: list[FileChunk] = []
        for ordinal, start in enumerate(range(0, len(text), max_characters)):
            content = text[start : start + max_characters]
            values.append(
                FileChunk(
                    id=hashlib.sha256(f"{ordinal}:{content}".encode("utf-8")).hexdigest(),
                    attachment_id="",
                    ordinal=ordinal,
                    content=content,
                    token_count=max(1, len(content.split())),
                    locator=FileLocator({"start": start, "end": start + len(content)}),
                    metadata=FileMetadata(),
                )
            )
        return values

    return chunk


class DisabledVectorIndexer:
    """Target-owned disabled indexer for deployments without embeddings."""

    async def index(self, attachment: Attachment, chunks: list[FileChunk]) -> None:
        return None

    async def delete(self, attachment_id: str) -> None:
        return None


class DisabledGraphIndexer:
    """Target-owned disabled graph indexer for deployments without Neo4j."""

    async def index(self, attachment: Attachment, chunks: list[FileChunk]) -> None:
        return None

    async def delete(self, attachment_id: str) -> None:
        return None


# Kept as import aliases for existing target tests during the transition. The
# production dependency graph uses the explicit ``Disabled*`` names only when
# an external index is deliberately disabled.
NoopVectorIndexer = DisabledVectorIndexer
NoopGraphIndexer = DisabledGraphIndexer


__all__ = [
    "NativeFileAdapterRegistry",
    "NativeTextParser",
    "DisabledGraphIndexer",
    "DisabledVectorIndexer",
    "NoopGraphIndexer",
    "NoopVectorIndexer",
    "line_chunker",
]
