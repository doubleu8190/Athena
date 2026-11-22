"""会话文件上传、文本分页与授权读取单元测试。"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from app.file import FileInputError, SessionFileService
from app.file import service as file_service_module
from common.json_stream import JsonStreamValidationCancelled
from domain.file import (
    MAX_SESSION_FILE_BYTES,
    FileObjectScanEntry,
    PendingSessionFile,
    SessionFile,
    SessionFilePage,
    SessionFileSource,
    TextFileReadError,
)

NOW = datetime(2026, 11, 12, tzinfo=UTC)


class _Objects:
    """保存内存对象并记录写入和删除次数。"""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.puts = 0
        self.deletes: list[str] = []

    async def start(self) -> None:
        return None

    async def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str,
    ) -> None:
        del content_type
        self.puts += 1
        self.values[key] = path.read_bytes()

    async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
        del content_type
        self.puts += 1
        self.values[key] = content

    async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
        content = self.values[key]
        if len(content) > maximum_bytes:
            raise ValueError("object_too_large")
        return content

    async def get_range(self, key: str, *, offset: int, length: int) -> bytes:
        return self.values[key][offset : offset + length]

    async def iter_range(
        self,
        key: str,
        *,
        offset: int = 0,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        content = self.values[key]
        for start in range(offset, len(content), chunk_size):
            yield content[start : start + chunk_size]

    async def delete(self, key: str) -> None:
        self.deletes.append(key)
        self.values.pop(key, None)

    async def iter_maintenance_candidates(
        self,
        *,
        older_than_seconds: int = 86400,
        after_key: str | None = None,
        maximum_items: int = 1000,
    ) -> AsyncIterator[FileObjectScanEntry]:
        del older_than_seconds, after_key, maximum_items
        if False:
            yield FileObjectScanEntry("session-files/00/empty", False)

    async def aclose(self) -> None:
        return None


class _Repository:
    """提供 owner/session 范围读取的最小内存仓储。"""

    def __init__(self, objects: _Objects) -> None:
        self.objects = objects
        self.files: dict[str, SessionFile] = {}
        self.idempotency: dict[tuple[str, str, str], SessionFile] = {}

    async def publish_upload(
        self,
        session_id: str,
        owner_id: str,
        file: PendingSessionFile,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        published_at: datetime,
    ) -> SessionFile:
        del published_at
        key = (session_id, owner_id, idempotency_key)
        existing = self.idempotency.get(key)
        if existing is not None:
            if existing.object_key != file.object_key:
                raise ValueError("idempotency_conflict")
            return existing
        result = SessionFile(
            id=file.id,
            session_id=session_id,
            owner_id=owner_id,
            source=SessionFileSource.UPLOAD,
            filename=file.filename,
            label=file.label,
            mime_type=file.mime_type,
            size_bytes=file.size_bytes,
            sha256=file.sha256,
            object_key=file.object_key,
            source_call_id=None,
            output_index=None,
            created_at=file.created_at,
        )
        _ = request_fingerprint
        self.idempotency[key] = result
        self.files[result.id] = result
        return result

    async def find_upload_request(
        self,
        session_id: str,
        owner_id: str,
        idempotency_key: str,
    ) -> SessionFile | None:
        return self.idempotency.get((session_id, owner_id, idempotency_key))

    async def list_visible(
        self,
        session_id: str,
        owner_id: str,
        *,
        after_file_id: str | None,
        limit: int,
    ) -> SessionFilePage | None:
        del after_file_id
        items = tuple(
            item
            for item in self.files.values()
            if item.session_id == session_id and item.owner_id == owner_id
        )
        return SessionFilePage(items=items[:limit], next_after_file_id=None)

    async def get_visible(
        self,
        session_id: str,
        owner_id: str,
        file_id: str,
    ) -> SessionFile | None:
        item = self.files.get(file_id)
        if (
            item is None
            or item.session_id != session_id
            or item.owner_id != owner_id
        ):
            return None
        return item

    async def get_visible_many(
        self,
        session_id: str,
        owner_id: str,
        file_ids: tuple[str, ...],
    ) -> tuple[SessionFile, ...]:
        if len(file_ids) != len(set(file_ids)):
            raise ValueError("session_file_ids_duplicate")
        values: list[SessionFile | None] = []
        for file_id in file_ids:
            values.append(await self.get_visible(session_id, owner_id, file_id))
        if any(item is None for item in values):
            raise ValueError("session_file_not_found")
        return tuple(item for item in values if item is not None)


def _service(objects: _Objects, repository: _Repository) -> SessionFileService:
    """创建使用固定 ID 与时钟的文件服务。"""
    ids = iter(("file_one", "file_two"))
    return SessionFileService(
        repository,
        objects,
        id_factory=lambda _: next(ids),
        clock=lambda: NOW,
    )


def test_upload_is_idempotent_and_does_not_write_second_object(tmp_path: Path) -> None:
    path = tmp_path / "sales.csv"
    path.write_text("month,total\n2026-10,42\n", encoding="utf-8")
    objects = _Objects()
    repository = _Repository(objects)
    service = _service(objects, repository)

    async def scenario() -> None:
        first = await service.upload(
            "ses_one",
            "usr_one",
            idempotency_key="upload-1",
            filename="sales.csv",
            declared_mime="text/csv",
            path=path,
        )
        replay = await service.upload(
            "ses_one",
            "usr_one",
            idempotency_key="upload-1",
            filename="sales.csv",
            declared_mime="text/csv",
            path=path,
        )
        assert replay == first
        assert first.source is SessionFileSource.UPLOAD
        assert first.output_index is None
        assert objects.puts == 1

    asyncio.run(scenario())


def test_text_read_preserves_utf8_boundaries_and_binary_metadata(tmp_path: Path) -> None:
    objects = _Objects()
    repository = _Repository(objects)
    service = _service(objects, repository)
    content = "A😀B".encode()
    objects.values["session-files/text"] = content
    text_file = SessionFile(
        id="file_text",
        session_id="ses_one",
        owner_id="usr_one",
        source=SessionFileSource.UPLOAD,
        filename="notes.txt",
        label=None,
        mime_type="text/plain",
        size_bytes=len(content),
        sha256="a" * 64,
        object_key="session-files/text",
        source_call_id=None,
        output_index=None,
        created_at=NOW,
    )
    repository.files[text_file.id] = text_file

    async def scenario() -> None:
        slice_one = await service.read_text(
            "ses_one",
            "usr_one",
            "file_text",
            offset=1,
            limit_bytes=4,
        )
        assert slice_one.content == "😀"
        assert slice_one.next_offset == 5
        assert slice_one.truncated is True

        with pytest.raises(TextFileReadError, match="file_offset_invalid"):
            await service.read_text(
                "ses_one",
                "usr_one",
                "file_text",
                offset=2,
                limit_bytes=4,
            )
        with pytest.raises(TextFileReadError, match="file_range_too_small"):
            await service.read_text(
                "ses_one",
                "usr_one",
                "file_text",
                offset=1,
                limit_bytes=2,
            )

        binary = replace(
            text_file,
            id="file_png",
            filename="chart.png",
            mime_type="image/png",
        )
        repository.files[binary.id] = binary
        with pytest.raises(TextFileReadError) as error:
            await service.read_text(
                "ses_one",
                "usr_one",
                "file_png",
                offset=0,
                limit_bytes=64,
            )
        assert error.value.code == "file_not_text"
        assert error.value.file == binary

    asyncio.run(scenario())


def test_upload_rejects_mime_spoof_and_cross_session_input(tmp_path: Path) -> None:
    script = tmp_path / "program.sh"
    script.write_text("#!/bin/sh\necho unsafe\n", encoding="utf-8")
    objects = _Objects()
    repository = _Repository(objects)
    service = _service(objects, repository)

    async def scenario() -> None:
        with pytest.raises(FileInputError, match="file_type_unsupported"):
            await service.upload(
                "ses_one",
                "usr_one",
                idempotency_key="upload-script",
                filename="program.sh",
                declared_mime="text/plain",
                path=script,
            )
        assert objects.puts == 0
        with pytest.raises(FileInputError, match="session_file_not_found"):
            await service.get_many("ses_two", "usr_one", ("missing",))

    asyncio.run(scenario())


def test_upload_committed_response_failure_replays_and_keeps_object(
    tmp_path: Path,
) -> None:
    """提交成功但回包异常时，独立重放仍能读到同一对象。"""

    class Repository(_Repository):
        async def publish_upload(self, *args: Any, **kwargs: Any) -> SessionFile:
            await super().publish_upload(*args, **kwargs)
            raise RuntimeError("response lost")

    path = tmp_path / "sales.csv"
    path.write_text("month,total\n2026-10,42\n", encoding="utf-8")
    objects = _Objects()
    repository = Repository(objects)
    service = _service(objects, repository)

    async def scenario() -> SessionFile:
        return await service.upload(
            "ses_one",
            "usr_one",
            idempotency_key="upload-replay",
            filename="sales.csv",
            declared_mime="text/csv",
            path=path,
        )

    uploaded = asyncio.run(scenario())
    assert uploaded.filename == "sales.csv"
    assert objects.deletes == []
    assert uploaded.object_key in objects.values


def test_upload_unknown_commit_visibility_retains_object_for_gc(tmp_path: Path) -> None:
    """异常后查询本身暂不可用，不得据一次空结果删除可能迟到的提交。"""

    class Repository(_Repository):
        finds = 0

        async def find_upload_request(
            self,
            session_id: str,
            owner_id: str,
            idempotency_key: str,
        ) -> SessionFile | None:
            self.finds += 1
            if self.finds == 1:
                return None
            raise RuntimeError("database read temporarily unavailable")

        async def publish_upload(self, *args: Any, **kwargs: Any) -> SessionFile:
            del args, kwargs
            raise RuntimeError("commit outcome unknown")

    path = tmp_path / "sales.csv"
    path.write_text("month,total\n2026-10,42\n", encoding="utf-8")
    objects = _Objects()
    repository = Repository(objects)
    service = _service(objects, repository)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="commit outcome unknown"):
            await service.upload(
                "ses_one",
                "usr_one",
                idempotency_key="upload-unknown",
                filename="sales.csv",
                declared_mime="text/csv",
                path=path,
            )

    asyncio.run(scenario())
    assert repository.finds == 2
    assert objects.puts == 1
    assert objects.deletes == []
    assert len(objects.values) == 1


def test_upload_eventually_visible_commit_is_not_deleted_by_early_miss(
    tmp_path: Path,
) -> None:
    """异常后的即时复核未命中时，稍后才可见的提交仍须保有原对象。"""

    class Repository(_Repository):
        def __init__(self, objects: _Objects) -> None:
            super().__init__(objects)
            self.pending_publish: tuple[
                tuple[Any, ...],
                dict[str, Any],
            ] | None = None
            self.commit_visible = False

        async def find_upload_request(
            self,
            session_id: str,
            owner_id: str,
            idempotency_key: str,
        ) -> SessionFile | None:
            if not self.commit_visible:
                return None
            return await super().find_upload_request(
                session_id,
                owner_id,
                idempotency_key,
            )

        async def publish_upload(
            self,
            *args: Any,
            **kwargs: Any,
        ) -> SessionFile:
            self.pending_publish = (args, kwargs)
            raise RuntimeError("commit outcome unknown")

        async def finish_delayed_commit(self) -> SessionFile:
            assert self.pending_publish is not None
            args, kwargs = self.pending_publish
            result = await super().publish_upload(*args, **kwargs)
            self.commit_visible = True
            return result

    path = tmp_path / "delayed.csv"
    path.write_text("month,total\n2026-11,43\n", encoding="utf-8")
    objects = _Objects()
    repository = Repository(objects)
    service = _service(objects, repository)

    async def scenario() -> SessionFile:
        with pytest.raises(RuntimeError, match="commit outcome unknown"):
            await service.upload(
                "ses_one",
                "usr_one",
                idempotency_key="upload-delayed",
                filename="delayed.csv",
                declared_mime="text/csv",
                path=path,
            )
        # 提交状态不明时对象保留；随后迟到的提交才让引用变为可见。
        assert repository.pending_publish is not None
        assert objects.deletes == []
        assert len(objects.values) == 1
        return await repository.finish_delayed_commit()

    published = asyncio.run(scenario())
    assert published.filename == "delayed.csv"
    assert objects.deletes == []
    assert published.object_key in objects.values


def test_upload_writes_object_before_reference_commit(
    tmp_path: Path,
) -> None:
    """先写对象，再由数据库事务提交唯一文件引用。"""
    events: list[str] = []

    class Objects(_Objects):
        async def put_file(
            self,
            key: str,
            path: Path,
            *,
            content_type: str,
        ) -> None:
            events.append("object")
            await super().put_file(key, path, content_type=content_type)

    class Repository(_Repository):
        async def publish_upload(self, *args: Any, **kwargs: Any) -> SessionFile:
            events.append("reference")
            return await super().publish_upload(*args, **kwargs)

    path = tmp_path / "guarded.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    objects = Objects()
    repository = Repository(objects)
    service = _service(objects, repository)

    asyncio.run(
        service.upload(
            "ses_one",
            "usr_one",
            idempotency_key="guarded",
            filename="guarded.csv",
            declared_mime="text/csv",
            path=path,
        )
    )

    assert events == ["object", "reference"]


def test_upload_limit_is_checked_before_hashing_or_storage(tmp_path: Path) -> None:
    """超过用户上传上限的文件在格式检查和完整摘要前即被拒绝。"""
    path = tmp_path / "oversized.csv"
    path.write_bytes(b"x")
    os.truncate(path, MAX_SESSION_FILE_BYTES + 1)
    objects = _Objects()
    service = _service(objects, _Repository(objects))

    async def scenario() -> None:
        with pytest.raises(FileInputError, match="document_file_too_large"):
            await service.upload(
                "ses_one",
                "usr_one",
                idempotency_key="oversized",
                filename="oversized.csv",
                declared_mime="text/csv",
                path=path,
            )

    asyncio.run(scenario())
    assert objects.puts == 0


def test_generated_artifact_exceeding_size_limit_is_rejected(tmp_path: Path) -> None:
    """脚本产物与用户上传统一执行 50 MiB 上限，超限必须在上传对象存储前被拒绝。"""

    class RejectingObjects(_Objects):
        async def put_file(
            self,
            key: str,
            path: Path,
            *,
            content_type: str,
        ) -> None:
            raise AssertionError("oversized artifact must not be uploaded")

    path = tmp_path / "large.csv"
    path.write_bytes(b"a")
    os.truncate(path, MAX_SESSION_FILE_BYTES + 1)
    objects = RejectingObjects()
    service = _service(objects, _Repository(objects))

    with pytest.raises(FileInputError, match="session_file_too_large"):
        asyncio.run(
            service.prepare_artifact(
                source_call_id="toolcall_" + "a" * 24,
                output_index=0,
                path=path,
                filename="large.csv",
                label="large",
            )
        )

    assert objects.puts == 0


@pytest.mark.parametrize("json_kind", ["scalar", "key", "nested"])
def test_generated_json_artifact_validation_uses_streaming_path_within_size_cap(
    tmp_path: Path,
    json_kind: str,
) -> None:
    """50 MiB 限额内的超大标量、大键和深嵌套 JSON 产物仍走流式校验发布。"""

    class StreamOnlyObjects(_Objects):
        async def put_file(
            self,
            key: str,
            path: Path,
            *,
            content_type: str,
        ) -> None:
            del content_type
            self.puts += 1
            self.values[key] = path.stat().st_size.to_bytes(8, "big")

    path = tmp_path / f"{json_kind}.json"
    if json_kind == "nested":
        path.write_bytes(b"[" * 150_000 + b"0" + b"]" * 150_000)
    else:
        prefix = b'{"' if json_kind == "key" else b'"'
        suffix = b'":0}' if json_kind == "key" else b'"'
        with path.open("wb") as stream:
            stream.write(prefix)
            chunk = b"a" * (1024 * 1024)
            for _ in range(2):
                stream.write(chunk)
            stream.write(suffix)
    objects = StreamOnlyObjects()
    service = _service(objects, _Repository(objects))

    artifact = asyncio.run(
        service.prepare_artifact(
            source_call_id=f"toolcall_{json_kind:0<24}",
            output_index=0,
            path=path,
            filename=path.name,
            label=json_kind,
        )
    )

    assert artifact.size_bytes == path.stat().st_size
    assert objects.puts == 1


def test_cancelled_artifact_preparation_does_not_publish_partial_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """磁盘检查在线程中执行，取消会停止并等待检查后再退出上传。"""
    started = threading.Event()

    def blocking_inspection(
        path: Path,
        filename: str,
        declared_mime: str | None,
        stop_event: threading.Event,
    ) -> file_service_module._FileInspection:
        del path, filename, declared_mime
        started.set()
        assert stop_event.wait(timeout=2)
        return file_service_module._FileInspection(1, "text/plain", "a" * 64)

    monkeypatch.setattr(file_service_module, "_inspect_file", blocking_inspection)
    objects = _Objects()
    service = _service(objects, _Repository(objects))
    path = tmp_path / "report.txt"
    path.write_text("x", encoding="utf-8")

    async def scenario() -> None:
        task = asyncio.create_task(
            service.prepare_artifact(
                source_call_id="toolcall_" + "b" * 24,
                output_index=0,
                path=path,
                filename="report.txt",
                label="report",
            )
        )
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert objects.puts == 0


@pytest.mark.parametrize("operation", ["upload", "artifact"])
def test_json_path_cancellation_uses_real_validator_and_joins_worker_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """上传和产物取消路径都打开真实文件并等待流式验证线程退出。"""

    class BlockingBinaryReader:
        """在首次读取前阻塞，确保取消发生在真实 JSON 验证过程中。"""

        def __init__(
            self,
            stream: Any,
            started: threading.Event,
            release: threading.Event,
        ) -> None:
            self._stream = stream
            self._started = started
            self._release = release
            self._blocked = False

        def read(self, size: int = -1) -> bytes:
            if not self._blocked:
                self._blocked = True
                self._started.set()
                assert self._release.wait(timeout=2)
            return cast(bytes, self._stream.read(size))

        def __enter__(self) -> BlockingBinaryReader:
            return self

        def __exit__(self, *args: object) -> None:
            del args
            self._stream.close()

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    path = tmp_path / "memory.json"
    path.write_text('{"value":1}', encoding="utf-8")
    original_open = Path.open
    original_validator = file_service_module.validate_json_stream

    def blocking_open(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> Any:
        stream = original_open(
            self,
            mode,
            buffering,
            encoding,
            errors,
            newline,
        )
        if self == path and mode == "rb":
            return BlockingBinaryReader(stream, started, release)
        return stream

    def observed_validator(
        stream: Any,
        *,
        cancellation: threading.Event | None = None,
    ) -> None:
        try:
            original_validator(stream, cancellation=cancellation)
        finally:
            finished.set()

    monkeypatch.setattr(Path, "open", blocking_open)
    monkeypatch.setattr(
        file_service_module,
        "validate_json_stream",
        observed_validator,
    )
    objects = _Objects()
    service = _service(objects, _Repository(objects))

    async def invoke() -> SessionFile | PendingSessionFile:
        if operation == "upload":
            return await service.upload(
                "ses_one",
                "usr_one",
                idempotency_key="cancel-real-path",
                filename="memory.json",
                declared_mime="application/json",
                path=path,
            )
        return await service.prepare_artifact(
            source_call_id="toolcall_" + "c" * 24,
            output_index=0,
            path=path,
            filename="memory.json",
            label="memory",
        )

    async def scenario() -> None:
        task = asyncio.create_task(invoke())
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert finished.is_set()
    assert objects.puts == 0


def test_real_large_text_validation_cancels_and_closes_worker_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """大文本解码在读取块边界观察到取消，外层等待线程关闭文件后退出。"""

    class BlockingBinaryReader:
        def __init__(
            self,
            stream: Any,
            started: threading.Event,
            release: threading.Event,
            closed: threading.Event,
        ) -> None:
            self._stream = stream
            self._started = started
            self._release = release
            self._closed = closed
            self._blocked = False

        def read(self, size: int = -1) -> bytes:
            if not self._blocked:
                self._blocked = True
                self._started.set()
                assert self._release.wait(timeout=2)
            return cast(bytes, self._stream.read(size))

        def __enter__(self) -> BlockingBinaryReader:
            return self

        def __exit__(self, *args: object) -> None:
            del args
            self._stream.close()
            self._closed.set()

    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    path = tmp_path / "large.txt"
    path.write_bytes(b"a" * (512 * 1024))
    original_open = Path.open

    def blocking_open(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> Any:
        stream = original_open(
            self,
            mode,
            buffering,
            encoding,
            errors,
            newline,
        )
        if self == path and mode == "rb":
            return BlockingBinaryReader(stream, started, release, closed)
        return stream

    monkeypatch.setattr(Path, "open", blocking_open)
    objects = _Objects()
    service = _service(objects, _Repository(objects))

    async def scenario() -> None:
        task = asyncio.create_task(
            service.upload(
                "ses_one",
                "usr_one",
                idempotency_key="cancel-large-text",
                filename="large.txt",
                declared_mime="text/plain",
                path=path,
            )
        )
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert closed.is_set()
    assert objects.puts == 0


def test_json_hash_cancellation_after_validation_closes_worker_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON 验证完成后仍可在摘要读取块中取消，线程必须退出。"""

    class BlockingHashReader:
        def __init__(
            self,
            stream: Any,
            started: threading.Event,
            release: threading.Event,
            closed: threading.Event,
        ) -> None:
            self._stream = stream
            self._started = started
            self._release = release
            self._closed = closed

        def read(self, size: int = -1) -> bytes:
            self._started.set()
            assert self._release.wait(timeout=2)
            return cast(bytes, self._stream.read(size))

        def __enter__(self) -> BlockingHashReader:
            return self

        def __exit__(self, *args: object) -> None:
            del args
            self._stream.close()
            self._closed.set()

    hash_started = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    path = tmp_path / "large.json"
    path.write_text('{"value":"ok"}', encoding="utf-8")
    original_open = Path.open
    binary_open_count = 0

    def blocking_hash_open(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> Any:
        nonlocal binary_open_count
        stream = original_open(
            self,
            mode,
            buffering,
            encoding,
            errors,
            newline,
        )
        if self == path and mode == "rb":
            binary_open_count += 1
            if binary_open_count == 2:
                return BlockingHashReader(
                    stream,
                    hash_started,
                    release,
                    closed,
                )
        return stream

    monkeypatch.setattr(Path, "open", blocking_hash_open)
    objects = _Objects()
    service = _service(objects, _Repository(objects))

    async def scenario() -> None:
        task = asyncio.create_task(
            service.upload(
                "ses_one",
                "usr_one",
                idempotency_key="cancel-json-hash",
                filename="large.json",
                declared_mime="application/json",
                path=path,
            )
        )
        assert await asyncio.to_thread(hash_started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert binary_open_count == 2
    assert closed.is_set()
    assert objects.puts == 0


@pytest.mark.parametrize(
    ("filename", "content", "expected"),
    [
        ("matrix.json", b'{"a":1,"b":[true,null]}', "application/json"),
        ("empty-array.json", b"[]", "application/json"),
    ],
)
def test_json_validation_accepts_single_streamed_root(
    tmp_path: Path,
    filename: str,
    content: bytes,
    expected: str,
) -> None:
    path = tmp_path / filename
    path.write_bytes(content)

    assert file_service_module.validate_file_type(path, filename, None) == expected


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("invalid.json", b'{"a":'),
        ("multiple.json", b"{} {}"),
        ("bad-utf8.json", b'"\xff"'),
        ("bad-utf8.txt", b"\xf0\x9f"),
    ],
)
def test_streaming_text_validation_rejects_invalid_content(
    tmp_path: Path,
    filename: str,
    content: bytes,
) -> None:
    path = tmp_path / filename
    path.write_bytes(content)

    with pytest.raises(FileInputError):
        file_service_module.validate_file_type(path, filename, None)


def test_json_upload_cancellation_stops_and_joins_worker_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消上传时必须设置线程停止事件并等待 JSON 检查线程退出。"""
    path = tmp_path / "large.json"
    path.write_text("{}", encoding="utf-8")
    started = threading.Event()
    stopped = threading.Event()
    finished = threading.Event()
    objects = _Objects()
    service = _service(objects, _Repository(objects))

    def blocking_validator(
        stream: Any,
        *,
        cancellation: threading.Event | None = None,
    ) -> None:
        del stream
        started.set()
        try:
            assert cancellation is not None
            while not cancellation.wait(0.005):
                pass
            stopped.set()
            raise JsonStreamValidationCancelled()
        finally:
            finished.set()

    monkeypatch.setattr(
        file_service_module,
        "validate_json_stream",
        blocking_validator,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            service.upload(
                "ses_one",
                "usr_one",
                idempotency_key="cancel-json",
                filename="large.json",
                declared_mime="application/json",
                path=path,
            )
        )
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert stopped.is_set()
    assert finished.is_set()
    assert objects.puts == 0
