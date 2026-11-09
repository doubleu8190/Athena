"""Pre-multipart request limits for knowledge file uploads."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from domain.knowledge.ports import KnowledgeRepositoryPort
from main import _KnowledgeUploadLimitMiddleware


def test_chunked_upload_without_content_length_is_bounded_before_parsing() -> None:
    async def scenario() -> None:
        calls = 0

        async def application(scope: object, receive: object, send: object) -> None:
            nonlocal calls
            calls += 1

        middleware = _KnowledgeUploadLimitMiddleware(application)
        messages = [
            {"type": "http.request", "body": b"x" * (11 * 1024 * 1024), "more_body": True},
            {"type": "http.request", "body": b"y" * (11 * 1024 * 1024), "more_body": True},
            {"type": "http.request", "body": b"z", "more_body": False},
        ]
        responses: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return messages.pop(0)

        async def send(message: dict[str, object]) -> None:
            responses.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/knowledge-bases/kb_1/documents/upload",
                "headers": [(b"content-type", b"multipart/form-data; boundary=x")],
            },
            receive,
            send,
        )
        assert calls == 0
        assert responses[0]["status"] == 413
        assert not messages

    asyncio.run(scenario())


def test_upload_slot_is_released_with_its_token_after_downstream() -> None:
    class Slots:
        acquired: list[tuple[str, int]]
        released: list[str]

        def __init__(self) -> None:
            self.acquired = []
            self.released = []

        async def acquire_upload_slot(self, token: str, *, lease_seconds: int) -> bool:
            self.acquired.append((token, lease_seconds))
            return True

        async def release_upload_slot(self, token: str) -> None:
            self.released.append(token)

    async def scenario() -> None:
        slots = Slots()
        app_calls = 0

        async def application(scope: object, receive: object, send: object) -> None:
            nonlocal app_calls
            app_calls += 1

        middleware = _KnowledgeUploadLimitMiddleware(
            application, cast(KnowledgeRepositoryPort, slots)
        )
        responses: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"ok", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            responses.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/knowledge-bases/kb_1/documents/upload",
            },
            receive,
            send,
        )
        assert app_calls == 1
        assert len(slots.acquired) == 1
        token, lease_seconds = slots.acquired[0]
        assert lease_seconds == 900
        assert slots.released == [token]

    asyncio.run(scenario())


def test_upload_admission_checks_only_the_temp_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_paths: list[str] = []
    temp_directory = "/tmp/athena-upload-test"

    def disk_usage(path: str) -> SimpleNamespace:
        checked_paths.append(path)
        return SimpleNamespace(free=1024**3)

    monkeypatch.setattr("main.shutil.disk_usage", disk_usage)
    monkeypatch.setattr("main.tempfile.gettempdir", lambda: temp_directory)

    class Slots:
        async def acquire_upload_slot(self, token: str, *, lease_seconds: int) -> bool:
            del token, lease_seconds
            return True

        async def release_upload_slot(self, token: str) -> None:
            del token

    async def scenario() -> None:
        async def application(scope: object, receive: object, send: object) -> None:
            del scope, receive, send

        middleware = _KnowledgeUploadLimitMiddleware(
            application, cast(KnowledgeRepositoryPort, Slots())
        )

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"ok", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            del message

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/knowledge-bases/kb_1/documents/upload",
            },
            receive,
            send,
        )

    asyncio.run(scenario())
    assert checked_paths == [temp_directory]


def test_upload_rejects_low_temp_space_before_acquiring_a_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "main.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )

    class Slots:
        async def acquire_upload_slot(self, token: str, *, lease_seconds: int) -> bool:
            raise AssertionError("low temp space must be rejected before slot acquisition")

        async def release_upload_slot(self, token: str) -> None:
            raise AssertionError("unacquired slot must not be released")

    async def scenario() -> None:
        app_calls = 0

        async def application(scope: object, receive: object, send: object) -> None:
            nonlocal app_calls
            app_calls += 1

        middleware = _KnowledgeUploadLimitMiddleware(
            application, cast(KnowledgeRepositoryPort, Slots())
        )
        responses: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            raise AssertionError("request body must not be read when temp space is low")

        async def send(message: dict[str, object]) -> None:
            responses.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/knowledge-bases/kb_1/documents/upload",
            },
            receive,
            send,
        )
        assert app_calls == 0
        assert responses[0]["status"] == 503

    asyncio.run(scenario())


def test_upload_is_rejected_when_shared_slot_pool_is_full() -> None:
    class FullSlots:
        async def acquire_upload_slot(self, token: str, *, lease_seconds: int) -> bool:
            return False

        async def release_upload_slot(self, token: str) -> None:
            raise AssertionError("unacquired slot must not be released")

    async def scenario() -> None:
        app_calls = 0

        async def application(scope: object, receive: object, send: object) -> None:
            nonlocal app_calls
            app_calls += 1

        middleware = _KnowledgeUploadLimitMiddleware(
            application, cast(KnowledgeRepositoryPort, FullSlots())
        )
        responses: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            raise AssertionError("request body must not be read without an upload slot")

        async def send(message: dict[str, object]) -> None:
            responses.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/knowledge-bases/kb_1/versions/upload",
            },
            receive,
            send,
        )
        assert app_calls == 0
        assert responses[0]["status"] == 429

    asyncio.run(scenario())
