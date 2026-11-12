"""停写窗口内的文件对象离线维护测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from app.file import FileObjectMaintenance, ObjectReclamationResult
from domain.file import FileObjectScanEntry


class _Store:
    """按对象键分页并记录删除结果的内存对象存储。"""

    def __init__(
        self,
        keys: tuple[str, ...],
        *,
        fail: set[str] | None = None,
        non_candidates: set[str] | None = None,
    ) -> None:
        self.keys = list(keys)
        self.fail = fail or set()
        self.non_candidates = non_candidates or set()
        self.deleted: list[str] = []
        self.retention: int | None = None
        self.cursors: list[str | None] = []
        self.maximums: list[int] = []

    async def iter_maintenance_candidates(
        self,
        *,
        older_than_seconds: int = 86400,
        after_key: str | None = None,
        maximum_items: int = 1000,
    ) -> AsyncIterator[FileObjectScanEntry]:
        self.retention = older_than_seconds
        self.cursors.append(after_key)
        self.maximums.append(maximum_items)
        examined = 0
        for key in sorted(self.keys):
            if after_key is not None and key <= after_key:
                continue
            if examined >= maximum_items:
                return
            examined += 1
            yield FileObjectScanEntry(
                object_key=key,
                is_candidate=key not in self.non_candidates,
            )

    async def delete(self, key: str) -> None:
        if key in self.fail:
            raise OSError("storage failed")
        self.deleted.append(key)
        self.keys.remove(key)


class _References:
    """返回固定引用集合，并可注入指定对象的查询失败。"""

    def __init__(
        self,
        referenced: set[str],
        *,
        fail: set[str] | None = None,
    ) -> None:
        self.referenced = referenced
        self.fail = fail or set()
        self.checked: list[str] = []

    async def is_object_key_referenced(self, object_key: str) -> bool:
        self.checked.append(object_key)
        if object_key in self.fail:
            raise RuntimeError("reference query failed")
        return object_key in self.referenced


def test_maintenance_defaults_to_dry_run_and_reports_candidates() -> None:
    store = _Store(
        (
            "session-files/aa/referenced",
            "session-files/bb/orphan",
            "operator-not-owned",
        ),
        non_candidates={"operator-not-owned"},
    )
    references = _References({"session-files/aa/referenced"})
    maintenance = FileObjectMaintenance(
        store=store,  # type: ignore[arg-type]
        references=references,
        page_size=2,
    )

    result = asyncio.run(maintenance.run())

    assert result == ObjectReclamationResult(
        examined_count=3,
        candidate_count=2,
        referenced_count=1,
        eligible_count=1,
        deleted_count=0,
        failed_count=0,
    )
    assert store.deleted == []
    assert store.retention == 86400
    assert store.cursors == [None, "session-files/aa/referenced"]
    assert store.maximums == [2, 2]


def test_maintenance_execute_requires_writer_stop_confirmation() -> None:
    maintenance = FileObjectMaintenance(
        store=_Store(()),  # type: ignore[arg-type]
        references=_References(set()),
    )

    with pytest.raises(ValueError, match="file_maintenance_writers_must_stop"):
        asyncio.run(maintenance.run(execute=True))


def test_maintenance_execute_deletes_only_unreferenced_candidates() -> None:
    store = _Store(
        (
            "session-files/aa/referenced",
            "session-files/bb/orphan-1",
            "session-files/cc/orphan-2",
            "session-files/dd/recent",
        ),
        non_candidates={"session-files/dd/recent"},
    )
    references = _References({"session-files/aa/referenced"})
    maintenance = FileObjectMaintenance(
        store=store,  # type: ignore[arg-type]
        references=references,
        page_size=2,
        delete_batch_size=1,
    )

    result = asyncio.run(
        maintenance.run(execute=True, writers_stopped=True)
    )

    assert result == ObjectReclamationResult(
        examined_count=4,
        candidate_count=3,
        referenced_count=1,
        eligible_count=2,
        deleted_count=2,
        failed_count=0,
    )
    assert store.deleted == [
        "session-files/bb/orphan-1",
        "session-files/cc/orphan-2",
    ]
    assert store.fail == set()


def test_reference_or_delete_failures_keep_objects_and_are_counted() -> None:
    store = _Store(
        (
            "session-files/aa/reference-error",
            "session-files/bb/delete-error",
            "session-files/cc/deleted",
        ),
        fail={"session-files/bb/delete-error"},
    )
    references = _References(
        set(),
        fail={"session-files/aa/reference-error"},
    )
    maintenance = FileObjectMaintenance(
        store=store,  # type: ignore[arg-type]
        references=references,
        delete_batch_size=2,
    )

    result = asyncio.run(
        maintenance.run(execute=True, writers_stopped=True)
    )

    assert result.failed_count == 2
    assert result.eligible_count == 2
    assert result.deleted_count == 1
    assert store.deleted == ["session-files/cc/deleted"]
    assert "session-files/aa/reference-error" in store.keys
    assert "session-files/bb/delete-error" in store.keys


def test_each_command_starts_from_bucket_beginning_without_persistent_cursor() -> None:
    keys = (
        "session-files/aa/orphan-1",
        "session-files/bb/referenced",
        "session-files/cc/orphan-2",
    )
    store = _Store(keys)
    references = _References({"session-files/bb/referenced"})
    maintenance = FileObjectMaintenance(
        store=store,  # type: ignore[arg-type]
        references=references,
        page_size=1,
    )

    first = asyncio.run(maintenance.run())
    second = asyncio.run(maintenance.run())

    assert first == second
    assert store.cursors == [
        None,
        keys[0],
        keys[1],
        keys[2],
        None,
        keys[0],
        keys[1],
        keys[2],
    ]


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("retention_seconds", 86399),
        ("page_size", 0),
        ("delete_batch_size", 101),
    ],
)
def test_maintenance_rejects_invalid_parameters(
    parameter: str,
    value: int,
) -> None:
    arguments = {
        "store": _Store(()),
        "references": _References(set()),
        parameter: value,
    }
    with pytest.raises(ValueError, match="file_maintenance_"):
        FileObjectMaintenance(**arguments)  # type: ignore[arg-type]
