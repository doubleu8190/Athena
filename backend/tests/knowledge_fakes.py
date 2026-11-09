"""In-memory original-store fake used only by tests."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from common.date import utc_now
from domain.knowledge.model import (
    OriginalStorageIntegrityError,
    OriginalStorageNotFoundError,
)
from domain.knowledge.ports import OriginalStore

type FakeStorageHook = Callable[[str, str | None, bytes | None], Awaitable[None]]


@dataclass(slots=True)
class FakeOriginal:
    content: bytes
    modified_at: datetime


class MemoryOriginalStore(OriginalStore):
    """Small deterministic implementation of the original-store port."""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._objects: dict[str, FakeOriginal] = {}
        self.put_calls: list[str] = []
        self.get_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.list_calls = 0
        self.operation_hook: FakeStorageHook | None = None

    async def _invoke_hook(
        self,
        operation: str,
        key: str | None = None,
        content: bytes | None = None,
    ) -> None:
        if self.operation_hook is not None:
            await self.operation_hook(operation, key, content)

    def seed(
        self,
        key: str,
        content: bytes,
        *,
        modified_at: datetime | None = None,
    ) -> None:
        self._objects[key] = FakeOriginal(content, modified_at or self._clock())

    def keys(self) -> set[str]:
        return set(self._objects)

    def contains(self, key: str) -> bool:
        return key in self._objects

    def content(self, key: str) -> bytes:
        return self._objects[key].content

    def modified_at(self, key: str) -> datetime:
        return self._objects[key].modified_at

    def set_modified_at(self, key: str, value: datetime) -> None:
        self._objects[key].modified_at = value

    async def put(self, key: str, content: bytes) -> None:
        self.put_calls.append(key)
        await self._invoke_hook("put", key, content)
        if hashlib.sha256(content).hexdigest() != key.rsplit("/", 1)[-1]:
            raise OriginalStorageIntegrityError("knowledge_storage_hash_mismatch")
        existing = self._objects.get(key)
        if existing is not None and existing.content != content:
            raise OriginalStorageIntegrityError("knowledge_storage_key_conflict")
        self._objects[key] = FakeOriginal(content, self._clock())

    async def get(self, key: str) -> bytes:
        self.get_calls.append(key)
        await self._invoke_hook("get", key)
        try:
            return self._objects[key].content
        except KeyError:
            raise OriginalStorageNotFoundError() from None

    async def delete(self, key: str) -> None:
        self.delete_calls.append(key)
        await self._invoke_hook("delete", key)
        self._objects.pop(key, None)

    async def iter_orphan_candidates(
        self, *, older_than_seconds: int = 86400
    ) -> AsyncIterator[str]:
        if older_than_seconds < 86400:
            raise ValueError("knowledge_orphan_grace_period_invalid")
        self.list_calls += 1
        await self._invoke_hook("list")
        cutoff = self._clock() - timedelta(seconds=older_than_seconds)
        for key in sorted(self._objects):
            if self._objects[key].modified_at < cutoff:
                await self._invoke_hook("list_item", key, self._objects[key].content)
                yield key


__all__ = ["FakeOriginal", "FakeStorageHook", "MemoryOriginalStore"]
