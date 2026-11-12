"""Office ZIP 的标准库类型、路径安全和取消边界测试。"""

from __future__ import annotations

import asyncio
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from docx import Document
from openpyxl import Workbook
from pptx import Presentation

from app.file import FileInputError, SessionFileService, validate_file_type
from app.file import service as file_service_module


def test_real_office_documents_keep_existing_type_validation(tmp_path: Path) -> None:
    """真实 Office 文件仍按扩展名、MIME 和内部 marker 返回规范类型。"""
    cases = (
        (
            "report.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            lambda path: Workbook().save(path),
        ),
        (
            "report.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            lambda path: Document().save(path),
        ),
        (
            "report.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            lambda path: Presentation().save(path),
        ),
    )
    for filename, expected, writer in cases:
        path = tmp_path / filename
        writer(path)
        assert validate_file_type(path, filename, None) == expected


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "../xl/workbook.xml",
        "/xl/workbook.xml",
        "\\xl\\workbook.xml",
        "xl//workbook.xml",
        "xl/./workbook.xml",
        "xl/bad\x00name.xml",
        "xl/bad\nname.xml",
    ],
)
def test_office_archive_rejects_unsafe_orig_filenames(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    """危险 `orig_filename` 必须在 marker 命中前被拒绝。"""
    path = tmp_path / "unsafe.xlsx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", b"types")
        archive.writestr(unsafe_name, b"root")

    with pytest.raises(FileInputError, match="file_content_mismatch"):
        validate_file_type(path, path.name, None)


@pytest.mark.parametrize(
    "members",
    [
        {"xl/workbook.xml": b"root"},
        {"[Content_Types].xml": b"types"},
    ],
)
def test_office_archive_requires_content_types_and_root_marker(
    tmp_path: Path,
    members: dict[str, bytes],
) -> None:
    path = tmp_path / "missing-marker.xlsx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)

    with pytest.raises(FileInputError, match="file_content_mismatch"):
        validate_file_type(path, path.name, None)


def test_office_archive_rejects_bad_zip(tmp_path: Path) -> None:
    path = tmp_path / "broken.docx"
    path.write_bytes(b"not-a-zip")

    with pytest.raises(FileInputError, match="file_content_mismatch"):
        validate_file_type(path, path.name, None)


def test_cancellation_after_zip_construction_stops_before_member_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ZipFile` 构造完成后立即观察取消，不再进入成员循环。"""
    path = tmp_path / "cancelled.xlsx"
    path.write_bytes(b"placeholder")
    cancellation = threading.Event()
    inspected_members: list[str] = []

    class Archive:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            cancellation.set()

        def __enter__(self) -> Archive:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def infolist(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(orig_filename="[Content_Types].xml"),
                SimpleNamespace(orig_filename="xl/workbook.xml"),
            ]

    monkeypatch.setattr(zipfile, "ZipFile", Archive)
    monkeypatch.setattr(
        file_service_module,
        "_validate_zip_member_path",
        lambda name: inspected_members.append(name),
    )

    with pytest.raises(file_service_module._FileInspectionCancelled):
        validate_file_type(
            path,
            path.name,
            None,
            cancellation=cancellation,
        )
    assert inspected_members == []


def test_api_cancellation_waits_zip_construction_and_never_writes_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上传取消会等待不可中断的中央目录构造，线程退出前不写对象。"""
    path = tmp_path / "cancel-during-construction.xlsx"
    path.write_bytes(b"placeholder")
    construction_started = threading.Event()
    allow_construction = threading.Event()

    class Store:
        writes = 0

        async def put_file(
            self,
            key: str,
            source: Path,
            *,
            content_type: str,
        ) -> None:
            del key, source, content_type
            self.writes += 1

    class Repository:
        lookups = 0

        async def find_upload_request(
            self,
            session_id: str,
            owner_id: str,
            idempotency_key: str,
        ) -> None:
            del session_id, owner_id, idempotency_key
            self.lookups += 1
            return None

    class Archive:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            construction_started.set()
            if not allow_construction.wait(timeout=3):
                raise TimeoutError("test did not release ZIP construction")

        def __enter__(self) -> Archive:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def infolist(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(orig_filename="[Content_Types].xml"),
                SimpleNamespace(orig_filename="xl/workbook.xml"),
            ]

    monkeypatch.setattr(zipfile, "ZipFile", Archive)
    store = Store()
    repository = Repository()
    service = SessionFileService(
        repository,  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            service.upload(
                "ses_office_cancel",
                "usr_office_cancel",
                idempotency_key="office-cancel",
                filename=path.name,
                declared_mime=None,
                path=path,
            )
        )
        await asyncio.to_thread(construction_started.wait, 3)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        allow_construction.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())
    assert store.writes == 0
    assert repository.lookups == 0
