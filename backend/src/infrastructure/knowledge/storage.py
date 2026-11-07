"""Local immutable original-file storage."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import time
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path

_KEY = re.compile(
    r"^(?:[a-f0-9]{64}/[a-f0-9]{2}/[a-f0-9]{64}|"
    r"[a-f0-9]{64}/[a-z0-9_-]{1,64}/[a-f0-9]{2}/[a-f0-9]{64})$"
)


class LocalOriginalStore:
    """Store files beneath a fixed root using content-addressed keys."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not _KEY.fullmatch(key):
            raise ValueError("knowledge_storage_key_invalid")
        path = self._root / key
        if path.is_symlink():
            raise ValueError("knowledge_storage_path_invalid")
        return path

    async def put(self, key: str, content: bytes) -> None:
        await self.put_stream(key, _single_chunk(content), max_bytes=len(content))

    async def put_stream(
        self, key: str, chunks: AsyncIterable[bytes], *, max_bytes: int
    ) -> tuple[str, int]:
        """Bound and atomically persist an async upload stream; return hash and size."""
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".upload-", dir=target.parent)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as output:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise ValueError("knowledge_upload_chunk_invalid")
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("knowledge_file_too_large")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            actual_hash = digest.hexdigest()
            if key.rsplit("/", 1)[-1] != actual_hash:
                raise ValueError("knowledge_storage_hash_mismatch")
            try:
                os.link(temp_name, target)
            except FileExistsError:
                if await asyncio.to_thread(_hash_file, target) != actual_hash:
                    raise ValueError("knowledge_storage_key_conflict") from None
            return actual_hash, size
        finally:
            Path(temp_name).unlink(missing_ok=True)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, missing_ok=True)

    async def sweep_orphans(
        self, referenced_keys: set[str], *, older_than_seconds: int = 86400
    ) -> int:
        """Remove only unreferenced immutable objects older than the grace period."""
        if older_than_seconds < 86400:
            raise ValueError("knowledge_orphan_grace_period_invalid")
        cutoff = time.time() - older_than_seconds
        removed = 0
        for path in await asyncio.to_thread(lambda: list(self._root.rglob("*"))):
            if not path.is_file():
                continue
            try:
                stat = await asyncio.to_thread(path.stat)
            except OSError:
                continue
            if stat.st_mtime >= cutoff:
                continue
            if path.name.startswith(".upload-"):
                await asyncio.to_thread(path.unlink, missing_ok=True)
                removed += 1
                continue
            key = path.relative_to(self._root).as_posix()
            try:
                self._path(key)
            except (OSError, ValueError):
                continue
            if key not in referenced_keys:
                await asyncio.to_thread(path.unlink, missing_ok=True)
                removed += 1
        return removed


async def _single_chunk(content: bytes) -> AsyncIterator[bytes]:
    yield content


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
