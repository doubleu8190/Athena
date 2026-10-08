"""原始文件内容存储端口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StoredBlob:
    """文件存储适配器返回的不可变 blob 信息。"""

    sha256: str
    size_bytes: int
    storage_key: str


class FileStoragePort(Protocol):
    """原始文件内容存储能力。"""

    async def save_stream(self, chunks: AsyncIterator[bytes]) -> StoredBlob: ...
    async def cleanup_unreferenced(self, live_storage_keys: set[str]) -> int: ...
    async def discard(self, storage_key: str) -> None: ...


__all__ = ["FileStoragePort", "StoredBlob"]
