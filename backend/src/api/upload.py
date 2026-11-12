"""HTTP multipart 文件的有界临时落盘。"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import UploadFile


class UploadTooLargeError(ValueError):
    """上传实际读取字节超过入口上限。"""


@asynccontextmanager
async def staged_upload(
    upload: UploadFile,
    *,
    maximum_bytes: int,
) -> AsyncIterator[Path]:
    """边读边计数到临时文件，并在所有退出路径删除。"""
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix="athena-upload-",
            suffix=".bin",
            delete=False,
        ) as staged:
            path = Path(staged.name)
            size = 0
            while chunk := await upload.read(128 * 1024):
                size += len(chunk)
                if size > maximum_bytes:
                    raise UploadTooLargeError("upload_too_large")
                staged.write(chunk)
            if size == 0:
                raise ValueError("upload_empty")
        assert path is not None
        yield path
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = ["UploadTooLargeError", "staged_upload"]
