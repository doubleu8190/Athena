"""文件适配器共享常量和辅助函数。"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from athena.utils.logging import get_logger

logger = get_logger(__name__)

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".rst",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".html",
    ".css",
    ".sql",
    ".log",
    ".ini",
    ".cfg",
}

CODE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "c_sharp",
}


def _decode(data: bytes) -> str:
    """自动检测文本编码并解码，失败时回退到 UTF-8。"""
    try:
        from charset_normalizer import from_bytes

        match = from_bytes(data).best()
        if match is not None:
            return str(match)
    except Exception as exc:
        logger.warning("charset_detection_failed", error=str(exc))
    return data.decode("utf-8", errors="replace")


def _rapidocr_text(img_content: str | bytes | Path) -> str:
    """使用 RapidOCR 从图片中提取文本。"""
    from rapidocr_onnxruntime import RapidOCR

    result, _ = RapidOCR()(img_content)
    return "\n".join(item[1] for item in (result or []))


def _pil_image_png_bytes(image: Any) -> bytes:
    """将 PIL 图像编码为 PNG 字节。"""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _frame_analysis(frame: Any) -> dict[str, Any]:
    """对 pandas DataFrame 执行列级统计分析。"""
    numeric = frame.select_dtypes(include="number")
    description = (
        numeric.describe().replace({float("nan"): None}).to_dict()
        if not numeric.empty
        else {}
    )
    return {
        "rows": int(len(frame)),
        "columns": [str(column) for column in frame.columns],
        "dtypes": {str(k): str(v) for k, v in frame.dtypes.items()},
        "missing": {str(k): int(v) for k, v in frame.isna().sum().items()},
        "description": description,
    }
