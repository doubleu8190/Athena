"""本地内容寻址文件存储。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import hashlib
from pathlib import Path

from domain.files import FileStoragePort, StoredBlob


class LocalFileStorage(FileStoragePort):
    """Store uploaded blobs in a local content-addressed directory."""

    def __init__(self, root: Path, max_upload_bytes: int) -> None:
        self._root = root
        self._max_upload_bytes = max_upload_bytes
        self._root.mkdir(parents=True, exist_ok=True)

    async def save_stream(self, chunks: AsyncIterator[bytes]) -> StoredBlob:
        content = bytearray()
        async for chunk in chunks:
            content.extend(chunk)
            if len(content) > self._max_upload_bytes:
                raise ValueError("upload exceeds maximum size")
        digest = hashlib.sha256(content).hexdigest()
        relative = Path(digest[:2]) / digest[2:]
        path = self._root / relative
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, content)
        return StoredBlob(digest, len(content), str(relative))

    async def cleanup_unreferenced(self, live_storage_keys: set[str]) -> int:
        removed = 0
        for path in self._root.rglob("*"):
            if not path.is_file():
                continue
            key = str(path.relative_to(self._root))
            if key not in live_storage_keys:
                path.unlink()
                removed += 1
        return removed

    async def discard(self, storage_key: str) -> None:
        path = self._root / storage_key
        if path.exists():
            path.unlink()

    async def read(self, storage_key: str) -> bytes:
        """Read a stored blob for a parser running in the same process."""
        path = self._root / storage_key
        exists = await asyncio.to_thread(path.is_file)
        if not exists:
            raise FileNotFoundError(storage_key)
        return await asyncio.to_thread(path.read_bytes)
