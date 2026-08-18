"""Configuration-driven adapter registry."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from athena.core.files.adapters import (
    CodeAdapter, ExcelAdapter, ImageAdapter, PdfAdapter, TextAdapter, WordAdapter,
)
from athena.core.files.base import FileAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        adapters: list[FileAdapter] = [
            PdfAdapter(), WordAdapter(), ExcelAdapter(), ImageAdapter(), CodeAdapter(), TextAdapter(),
        ]
        self._adapters = {adapter.info.name: adapter for adapter in adapters}

    def list(self) -> list[FileAdapter]:
        return list(self._adapters.values())

    def supported_extensions(self) -> list[str]:
        """Return every selectable extension supported by an enabled adapter."""
        return sorted({extension for adapter in self._adapters.values() for extension in adapter.info.extensions})

    def select(self, filename: str, mime_type: str = "") -> FileAdapter:
        lower = filename.lower()
        extension = Path(lower).suffix
        if extension in (".doc", ".xls"):
            target = "DOCX" if extension == ".doc" else "XLSX"
            raise ValueError(f"不支持旧版 {extension[1:].upper()} 格式，请先转换为 {target} 后重新上传")
        for adapter in self._adapters.values():
            if extension in adapter.info.extensions:
                mime_adapter = next(
                    (item for item in self._adapters.values() if mime_type in item.info.mime_types),
                    None,
                )
                if mime_adapter is not None and mime_adapter.info.name != adapter.info.name:
                    raise ValueError(f"文件扩展名与 MIME 类型不匹配: {filename} / {mime_type}")
                return adapter
        for adapter in self._adapters.values():
            if mime_type in adapter.info.mime_types:
                return adapter
        guessed = mimetypes.guess_type(filename)[0] or ""
        for adapter in self._adapters.values():
            if guessed in adapter.info.mime_types:
                return adapter
        raise ValueError(f"不支持的文件类型: {filename}")
