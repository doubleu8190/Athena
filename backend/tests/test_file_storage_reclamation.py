"""文件对象离线维护扫描的有界页与候选标记测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from adapter.file import MinioFileObjectStore, MinioFileStorageConfig

NOW = datetime(2026, 11, 12, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Item:
    object_name: str
    last_modified: datetime


class _Client:
    def __init__(self, items: tuple[_Item, ...]) -> None:
        self.items = items
        self.list_calls: list[str | None] = []

    def list_objects(
        self,
        bucket_name: str,
        *,
        recursive: bool,
        start_after: str | None = None,
    ) -> object:
        del bucket_name, recursive
        self.list_calls.append(start_after)
        return iter(
            item
            for item in self.items
            if start_after is None or item.object_name > start_after
        )


def _store(client: _Client) -> MinioFileObjectStore:
    return MinioFileObjectStore(
        MinioFileStorageConfig(
            endpoint="minio.example.test:443",
            access_key="test-access",
            secret_key="test-secret",
            bucket="athena-files-test",
            secure=True,
        ),
        client=client,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


def test_storage_returns_bounded_raw_items_and_marks_ineligible_objects() -> None:
    """未到期和非法键也返回为已扫描项，页大小受回收检查上限约束。"""
    items = (
        _Item("session-files/aa/old", NOW.replace(day=1)),
        _Item("session-files/bb/recent", NOW),
        _Item("operator-not-owned", NOW.replace(day=1)),
        _Item("session-files/cc/not-read", NOW.replace(day=1)),
    )
    client = _Client(items)

    async def scenario() -> list[object]:
        return [
            entry
            async for entry in _store(client).iter_maintenance_candidates(
                older_than_seconds=86400,
                maximum_items=3,
            )
        ]

    entries = asyncio.run(scenario())

    assert [entry.object_key for entry in entries] == [
        "session-files/aa/old",
        "session-files/bb/recent",
        "operator-not-owned",
    ]
    assert [entry.is_candidate for entry in entries] == [True, False, False]
    assert client.list_calls == [None]


@pytest.mark.parametrize("maximum_items", [0, 1001])
def test_storage_rejects_invalid_maintenance_page_size(maximum_items: int) -> None:
    async def scenario() -> None:
        iterator = _store(_Client(())).iter_maintenance_candidates(
            maximum_items=maximum_items
        )
        await anext(iterator)

    with pytest.raises(ValueError, match="file_maintenance_page_size_invalid"):
        asyncio.run(scenario())
