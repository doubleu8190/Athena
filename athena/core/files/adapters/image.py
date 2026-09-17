"""图片文件适配器。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from athena.config.settings import Settings
from athena.core.files.extraction import (
    ExtractedUnit,
    ExtractionContext,
    ExtractionResult,
)
from athena.models.file import AdapterInfo

from .common import _rapidocr_text, logger


class ImageAdapter:
    """图片文件适配器，支持尺寸信息和 OCR 文本提取。"""

    info = AdapterInfo(
        name="image",
        version="2.0",
        mime_types=["image/png", "image/jpeg", "image/webp", "image/tiff"],
        extensions=[".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"],
        capabilities=["read", "search", "summarize", "analyze"],
    )

    async def extract(
        self, context: ExtractionContext, settings: Settings
    ) -> ExtractionResult:
        """提取图片 OCR 文本。"""
        return await asyncio.to_thread(self._extract, context.path)

    def _extract(self, path: Path) -> ExtractionResult:
        """同步提取图片元数据和 OCR 文本。"""
        from PIL import Image

        with Image.open(path) as image:
            width, height, mode = image.width, image.height, image.mode
        text = ""
        try:
            text = _rapidocr_text(path)
        except Exception as exc:
            logger.warning("image_ocr_failed", path=str(path), error=str(exc))
        return ExtractionResult(
            units=(
                [ExtractedUnit(text, {"image": path.name}, {"kind": "ocr"})]
                if text
                else []
            ),
            metadata={
                "width": width,
                "height": height,
                "mode": mode,
                "ocr_available": bool(text),
            },
        )

    async def analyze(self, path: Path, task: str) -> dict[str, Any]:
        """分析图片并返回尺寸、模式和 OCR 文本。"""
        result = await asyncio.to_thread(self._extract, path)
        ocr_text = "\n".join(unit.content for unit in result.units).strip()
        analysis: dict[str, Any] = {
            **result.metadata,
            "note": "适配器仅提供图片元数据和 OCR 文本；视觉描述由 runtime 根据模型能力处理。",
        }
        if ocr_text:
            analysis["ocr_text"] = ocr_text
        return analysis
