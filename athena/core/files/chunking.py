"""Content-unit chunking for File Intelligence.

The chunker is deliberately independent from repositories, adapters, and
events.  It turns extracted units into persisted ``FileChunk`` values while
keeping locator and overlap semantics in one place.
"""

from __future__ import annotations

import re
from typing import Any

from athena.core.files.extraction import ExtractedUnit
from athena.core.llm.tokens import EmbeddingTokenCounter
from athena.models.file import FileChunk
from athena.models.json_models import FileLocator, FileMetadata
from athena.utils.id_generation import generate_time_id


class FileChunker:
    """Split extracted units into token-bounded, locatable chunks."""

    def __init__(self, token_counter: EmbeddingTokenCounter) -> None:
        self._token_counter = token_counter

    def chunk(
        self,
        attachment_id: str,
        units: list[ExtractedUnit],
        *,
        max_tokens: int,
        overlap_tokens: int,
    ) -> list[FileChunk]:
        max_tokens = max(1, max_tokens)
        overlap_tokens = min(max_tokens // 3, max(0, overlap_tokens))
        chunks: list[FileChunk] = []
        ordinal = 0
        for group in self.group_units(units):
            content = "\n\n".join(unit.content for unit in group).replace("\x00", "").strip()
            if not content:
                continue
            start = 0
            while start < len(content):
                end = self.find_chunk_end(content, start, max_tokens)
                end = self.prefer_semantic_boundary(content, start, end)
                piece = content[start:end].strip()
                if piece:
                    chunks.append(
                        FileChunk(
                            id=generate_time_id(),
                            attachment_id=attachment_id,
                            ordinal=ordinal,
                            content=piece,
                            token_count=self._token_counter.count_text_tokens(piece),
                            locator=FileLocator.model_validate(
                                self.chunk_locator(group, start, end)
                            ),
                            metadata=FileMetadata.model_validate(
                                self.group_metadata(group)
                            ),
                        )
                    )
                    ordinal += 1
                if end >= len(content):
                    break
                start = max(
                    start + 1,
                    self.find_overlap_start(content, start, end, overlap_tokens),
                )
        return chunks

    @staticmethod
    def group_units(units: list[ExtractedUnit]) -> list[list[ExtractedUnit]]:
        groups: list[list[ExtractedUnit]] = []
        current: list[ExtractedUnit] = []
        for unit in units:
            is_pdf_page = unit.kind == "page" or unit.metadata.get("kind") == "page"
            if current and (
                not is_pdf_page
                or not current[-1].can_merge_after
                or not unit.can_merge_before
            ):
                groups.append(current)
                current = []
            if not is_pdf_page:
                groups.append([unit])
            else:
                current.append(unit)
        if current:
            groups.append(current)
        return groups

    @staticmethod
    def group_metadata(group: list[ExtractedUnit]) -> dict[str, Any]:
        if len(group) == 1:
            return dict(group[0].metadata)
        return {
            "kind": "pdf_document",
            "ocr": any(bool(unit.metadata.get("ocr")) for unit in group),
            "chunk_strategy": "pdf-semantic-v1",
        }

    @staticmethod
    def chunk_locator(
        group: list[ExtractedUnit], start: int, end: int
    ) -> dict[str, Any]:
        if len(group) == 1:
            locator = dict(group[0].locator)
            page = locator.get("page")
            if page is not None:
                locator.setdefault("page_start", page)
                locator.setdefault("page_end", page)
            locator.update(char_start=start, char_end=end)
            return locator
        cursor = 0
        pages: list[int] = []
        for unit in group:
            unit_start = cursor
            cursor += len(unit.content) + 2
            unit_end = cursor
            if end > unit_start and start < unit_end:
                page = unit.locator.get("page")
                if isinstance(page, int):
                    pages.append(page)
        locator = {
            "page_start": min(pages) if pages else None,
            "page_end": max(pages) if pages else None,
            "char_start": start,
            "char_end": end,
        }
        return {key: value for key, value in locator.items() if value is not None}

    @staticmethod
    def prefer_semantic_boundary(content: str, start: int, end: int) -> int:
        if end >= len(content):
            return end
        midpoint = start + max(1, (end - start) // 2)
        newline = content.rfind("\n", midpoint, end)
        if newline > start:
            return newline
        matches = list(re.finditer(r"[。！？；!?;]\s*", content[midpoint:end]))
        return midpoint + matches[-1].end() if matches else end

    def find_chunk_end(self, content: str, start: int, max_tokens: int) -> int:
        if self._token_counter.count_text_tokens(content[start:]) <= max_tokens:
            return len(content)
        low = start + 1
        high = len(content)
        best = low
        while low <= high:
            middle = (low + high) // 2
            if self._token_counter.count_text_tokens(content[start:middle]) <= max_tokens:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        return best

    def find_overlap_start(
        self, content: str, chunk_start: int, chunk_end: int, overlap_tokens: int
    ) -> int:
        if overlap_tokens <= 0:
            return chunk_end
        low = chunk_start
        high = chunk_end
        best = chunk_end
        while low <= high:
            middle = (low + high) // 2
            if self._token_counter.count_text_tokens(content[middle:chunk_end]) <= overlap_tokens:
                best = middle
                high = middle - 1
            else:
                low = middle + 1
        return best


__all__ = ["FileChunker"]
