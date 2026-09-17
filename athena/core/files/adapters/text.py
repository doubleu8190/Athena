"""纯文本和 CSV 文件适配器。"""

from __future__ import annotations

import asyncio
import csv
import io
from pathlib import Path
from typing import Any

from athena.config.settings import Settings
from athena.core.files.extraction import (
    ExtractedUnit,
    ExtractionContext,
    ExtractionResult,
)
from athena.models.file import AdapterInfo

from .common import TEXT_EXTENSIONS, _decode, _frame_analysis


class TextAdapter:
    """纯文本和结构化文本文件适配器。"""

    info = AdapterInfo(
        name="text",
        version="2.0",
        mime_types=["text/plain", "text/markdown", "application/json"],
        extensions=sorted(TEXT_EXTENSIONS | {".csv"}),
        capabilities=["read", "search", "summarize", "analyze"],
    )

    async def extract(
        self, context: ExtractionContext, settings: Settings
    ) -> ExtractionResult:
        """提取文本文件内容。"""
        return await asyncio.to_thread(self._extract, context)

    def _extract(self, context: ExtractionContext) -> ExtractionResult:
        """同步提取文本内容和 CSV 表格数据。"""
        path = context.path
        filename = Path(context.filename).name
        suffix = Path(filename).suffix.lower()
        text = _decode(path.read_bytes())
        metadata: dict[str, Any] = {}
        tables: list[dict[str, Any]] = []
        if suffix == ".csv":
            rows = list(csv.reader(io.StringIO(text)))
            if rows:
                metadata.update(
                    {"row_count": max(0, len(rows) - 1), "columns": rows[0]}
                )
                tables.append(
                    {"name": filename, "headers": rows[0], "rows": rows[1:201]}
                )
        else:
            metadata.update({"line_count": len(text.splitlines())})
        return ExtractionResult(
            units=[
                ExtractedUnit(
                    text,
                    {"path": path.name, "start_line": 1},
                    {
                        "kind": "table" if suffix == ".csv" else "text",
                        "format": suffix[1:] if suffix else "text",
                    },
                )
            ],
            metadata=metadata,
            tables=tables,
        )

    async def analyze(self, path: Path, task: str) -> dict[str, Any]:
        """分析文本文件。"""
        text = _decode(path.read_bytes())
        if path.suffix.lower() != ".csv":
            return {"characters": len(text), "lines": len(text.splitlines())}
        try:
            import pandas as pd

            frame = await asyncio.to_thread(pd.read_csv, path)
            return _frame_analysis(frame)
        except Exception as exc:
            return {"error": str(exc)}
