"""Pre-multipart request limits for knowledge file uploads."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from domain.knowledge.ports import KnowledgeRepositoryPort
from domain.skill import MAX_PERSONAL_MARKDOWN_BYTES
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


def test_personal_markdown_chunked_upload_is_bounded_before_parsing() -> None:
    """个人 Markdown 在 multipart 解析前按原始分块计数并返回稳定 413。"""

    async def scenario() -> None:
        calls = 0

        async def application(scope: object, receive: object, send: object) -> None:
            nonlocal calls
            calls += 1

        middleware = _KnowledgeUploadLimitMiddleware(application)
        messages = [
            {"type": "http.request", "body": b"x" * 600_000, "more_body": True},
            {"type": "http.request", "body": b"y" * 600_000, "more_body": True},
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
                "path": "/api/v1/me/skills/parse",
                "headers": [(b"content-type", b"multipart/form-data; boundary=x")],
            },
            receive,
            send,
        )
        assert MAX_PERSONAL_MARKDOWN_BYTES < 1_200_000
        assert calls == 0
        assert responses[0]["status"] == 413
        body = responses[-1]["body"]
        assert isinstance(body, bytes)
        assert b'"detail":"skill_markdown_too_large"' in body

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/me/skills",
        "/api/v1/me/skills/skill_demo/versions",
    ),
)
def test_personal_json_chunked_write_is_bounded_before_parsing(path: str) -> None:
    """个人创建和发布 JSON 在无 Content-Length 的分块请求中同样受限。"""

    async def scenario() -> None:
        calls = 0

        async def application(scope: object, receive: object, send: object) -> None:
            nonlocal calls
            calls += 1

        middleware = _KnowledgeUploadLimitMiddleware(application)
        messages = [
            {"type": "http.request", "body": b"{" + b" " * (1024 * 1024), "more_body": True},
            {
                "type": "http.request",
                "body": b" " * (1024 * 1024) + b"}",
                "more_body": False,
            },
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
                "path": path,
                "headers": [(b"content-type", b"application/json")],
            },
            receive,
            send,
        )
        assert calls == 0
        assert responses[0]["status"] == 413
        body = responses[-1]["body"]
        assert isinstance(body, bytes)
        assert b'"detail":"skill_personal_request_too_large"' in body

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


def test_upload_middleware_rejects_unauthenticated_before_disk_slot_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启用鉴权器时，未登录或无效 Cookie 在磁盘检查、槽位占用与读体前直接返回 401。"""
    from domain.auth import AuthContext

    monkeypatch.setattr(
        "main.shutil.disk_usage",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("disk_usage must not be checked before auth")
        ),
    )

    class _Slots:
        async def acquire_upload_slot(self, token: str, *, lease_seconds: int) -> bool:
            raise AssertionError("slot must not be acquired before auth")

        async def release_upload_slot(self, token: str) -> None:
            raise AssertionError("slot must not be released")

    class _Authenticator:
        def __init__(self) -> None:
            self.seen_tokens: list[str] = []

        async def authenticate(self, token: str) -> AuthContext | None:
            self.seen_tokens.append(token)
            return None

    async def scenario() -> None:
        auth = _Authenticator()
        app_calls = 0

        async def application(scope: object, receive: object, send: object) -> None:
            nonlocal app_calls
            app_calls += 1

        from domain.knowledge.ports import KnowledgeUploadSlotRepository

        middleware = _KnowledgeUploadLimitMiddleware(
            application,
            cast(KnowledgeUploadSlotRepository, _Slots()),
            authenticator=auth,
        )

        async def receive() -> dict[str, object]:
            raise AssertionError("request body must not be read for unauthenticated request")

        for headers in (
            [],
            [(b"cookie", b"athena_session=expired_or_invalid")],
        ):
            responses: list[dict[str, object]] = []

            async def send(
                message: dict[str, object],
                sink: list[dict[str, object]] = responses,
            ) -> None:
                sink.append(message)

            await middleware(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/v1/knowledge-bases/kb_1/documents/upload",
                    "headers": headers,
                },
                receive,
                send,
            )
            assert responses[0]["status"] == 401
            assert responses[-1]["body"] == b'{"detail":"not_authenticated"}'

        assert app_calls == 0
        assert auth.seen_tokens == ["expired_or_invalid"]

    asyncio.run(scenario())


def test_upload_middleware_enforces_admin_role_on_skill_uploads_and_handles_db_error() -> None:
    """管理员技能上传预检非管理员返回 403；鉴权数据库故障安全返回 503。"""
    from datetime import UTC, datetime

    from sqlalchemy.exc import OperationalError

    from domain.auth import AuthContext, User, UserRole, UserStatus

    now = datetime(2026, 11, 22, tzinfo=UTC)

    class _Authenticator:
        async def authenticate(self, token: str) -> AuthContext | None:
            if token == "db_down":
                raise OperationalError("SELECT 1", {}, Exception("connection refused"))
            if token == "member_token":
                return AuthContext(
                    user=User(
                        id="usr_member",
                        email="member@example.test",
                        password_hash="h",
                        display_name=None,
                        status=UserStatus.ACTIVE,
                        role=UserRole.USER,
                        created_at=now,
                        updated_at=now,
                    ),
                    session_id="ses_member",
                )
            if token == "admin_token":
                return AuthContext(
                    user=User(
                        id="usr_admin",
                        email="admin@example.test",
                        password_hash="h",
                        display_name=None,
                        status=UserStatus.ACTIVE,
                        role=UserRole.ADMIN,
                        created_at=now,
                        updated_at=now,
                    ),
                    session_id="ses_admin",
                )
            return None

    async def scenario() -> None:
        app_calls = 0

        async def application(scope: object, receive: object, send: object) -> None:
            nonlocal app_calls
            app_calls += 1

        middleware = _KnowledgeUploadLimitMiddleware(
            application,
            authenticator=_Authenticator(),
        )

        async def fail_receive() -> dict[str, object]:
            raise AssertionError("body must not be read when rejected")

        # 1. 普通用户请求管理员技能导入 -> 403 admin_required
        member_responses: list[dict[str, object]] = []

        async def send_member(message: dict[str, object]) -> None:
            member_responses.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/admin/skills/import",
                "headers": [(b"cookie", b"bad[cookie]=1; athena_session=member_token")],
            },
            fail_receive,
            send_member,
        )
        assert member_responses[0]["status"] == 403
        assert member_responses[-1]["body"] == b'{"detail":"admin_required"}'

        # 2. 鉴权遇 SQLAlchemyError -> 503 database_unavailable
        db_responses: list[dict[str, object]] = []

        async def send_db(message: dict[str, object]) -> None:
            db_responses.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/admin/skills/sk_1/versions",
                "headers": [(b"cookie", b"athena_session=db_down")],
            },
            fail_receive,
            send_db,
        )
        assert db_responses[0]["status"] == 503
        assert db_responses[-1]["body"] == b'{"detail":"database_unavailable"}'
        assert app_calls == 0

        # 3. 管理员凭据允许进入下游
        admin_responses: list[dict[str, object]] = []

        async def send_admin(message: dict[str, object]) -> None:
            admin_responses.append(message)

        async def ok_receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"zip", "more_body": False}

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/admin/skills/import",
                "headers": [(b"cookie", b"athena_session=admin_token")],
            },
            ok_receive,
            send_admin,
        )
        assert app_calls == 1

    asyncio.run(scenario())


def test_upload_middleware_isolates_local_slots_per_kind_and_fails_fast_with_429() -> None:
    """不同 upload_kind 本地槽位互不阻塞，同类满载立即返回 429 而非无限排队。"""

    async def scenario() -> None:
        release_skills = asyncio.Event()
        entered_skills = 0
        skill_slots_full = asyncio.Event()
        session_file_calls = 0

        async def application(scope: dict[str, object], receive: object, send: object) -> None:
            nonlocal entered_skills, session_file_calls
            del receive, send
            path = str(scope.get("path", ""))
            if path.startswith("/api/v1/admin/skills/"):
                entered_skills += 1
                if entered_skills == 2:
                    skill_slots_full.set()
                await release_skills.wait()
            else:
                session_file_calls += 1

        middleware = _KnowledgeUploadLimitMiddleware(application)

        async def ok_receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"ok", "more_body": False}

        async def noop_send(message: dict[str, object]) -> None:
            del message

        # 占满 skill 的 2 个本地槽位
        t1 = asyncio.create_task(
            middleware(
                {"type": "http", "method": "POST", "path": "/api/v1/admin/skills/import"},
                ok_receive,
                noop_send,
            )
        )
        t2 = asyncio.create_task(
            middleware(
                {"type": "http", "method": "POST", "path": "/api/v1/admin/skills/import"},
                ok_receive,
                noop_send,
            )
        )
        await asyncio.wait_for(skill_slots_full.wait(), timeout=2)

        # 第 3 个 skill 请求必须立即快速返回 429，不读取请求体
        rejected_responses: list[dict[str, object]] = []

        async def send_rejected(message: dict[str, object]) -> None:
            rejected_responses.append(message)

        async def fail_receive() -> dict[str, object]:
            raise AssertionError("body must not be read when slot is full")

        await middleware(
            {"type": "http", "method": "POST", "path": "/api/v1/admin/skills/import"},
            fail_receive,
            send_rejected,
        )
        assert rejected_responses[0]["status"] == 429
        assert rejected_responses[-1]["body"] == b'{"detail":"skill_upload_capacity_exceeded"}'

        # 与此同时，session_file 槽位互不影响，正常放行
        await middleware(
            {"type": "http", "method": "POST", "path": "/api/v1/sessions/ses_1/files"},
            ok_receive,
            noop_send,
        )
        assert session_file_calls == 1

        release_skills.set()
        await asyncio.gather(t1, t2)

        # 释放后 skill 上传恢复可用
        await middleware(
            {"type": "http", "method": "POST", "path": "/api/v1/admin/skills/import"},
            ok_receive,
            noop_send,
        )
        assert entered_skills == 3

    asyncio.run(scenario())


def test_upload_middleware_returns_kind_specific_timeout_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上传超时按 upload_kind 返回准确错误码，不再对所有路径恒返 knowledge_upload_timeout。"""
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def immediate_timeout(_seconds: float) -> AsyncIterator[None]:
        raise TimeoutError
        yield

    monkeypatch.setattr("main.asyncio.timeout", immediate_timeout)

    async def scenario() -> None:
        async def application(scope: object, receive: object, send: object) -> None:
            del scope, receive, send

        middleware = _KnowledgeUploadLimitMiddleware(application)

        async def ok_receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"ok", "more_body": False}

        cases = (
            (
                "/api/v1/knowledge-bases/kb_1/documents/upload",
                b'{"detail":"knowledge_upload_timeout"}',
            ),
            ("/api/v1/admin/skills/import", b'{"detail":"skill_upload_timeout"}'),
            ("/api/v1/sessions/ses_1/files", b'{"detail":"session_file_upload_timeout"}'),
            ("/api/v1/me/skills/parse", b'{"detail":"upload_timeout"}'),
        )
        for path, expected_body in cases:
            responses: list[dict[str, object]] = []

            async def send_timeout(
                message: dict[str, object],
                sink: list[dict[str, object]] = responses,
            ) -> None:
                sink.append(message)

            await middleware(
                {"type": "http", "method": "POST", "path": path},
                ok_receive,
                send_timeout,
            )
            assert responses[0]["status"] == 504
            assert responses[-1]["body"] == expected_body

    asyncio.run(scenario())
