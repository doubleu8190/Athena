"""上传准入中间件流式限流、并发槽位与临时落盘行为测试。"""

from __future__ import annotations

import asyncio
import io
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import pytest
from fastapi import FastAPI, File, UploadFile
from fastapi.testclient import TestClient

from api.upload import UploadTooLargeError, staged_upload
from api.upload_middleware import UploadAdmissionMiddleware
from domain.skill import MAX_PERSONAL_MARKDOWN_BYTES


async def _drain_request_body(receive: object) -> bytes:
    """模拟下游解析器持续拉取 ASGI 请求体分块。"""
    assert callable(receive)
    chunks: list[bytes] = []
    while True:
        message = await receive()
        assert isinstance(message, dict)
        if message["type"] == "http.disconnect":
            break
        chunk = message.get("body", b"")
        if isinstance(chunk, bytes):
            chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def test_content_length_fast_reject_skips_slot_and_body_read() -> None:
    """显式 Content-Length 超限时直接返回 413，不占槽位、不读请求体。"""

    async def scenario() -> None:
        calls = 0

        async def application(scope: object, receive: object, send: object) -> None:
            nonlocal calls
            del scope, receive, send
            calls += 1

        middleware = UploadAdmissionMiddleware(application)
        responses: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            raise AssertionError("request body must not be read on Content-Length fast-reject")

        async def send(message: dict[str, object]) -> None:
            responses.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/knowledge-bases/kb_1/documents/upload",
                "headers": [
                    (b"content-type", b"multipart/form-data; boundary=x"),
                    (b"content-length", str(22 * 1024 * 1024 + 1).encode("ascii")),
                ],
            },
            receive,
            send,
        )
        assert calls == 0
        assert middleware._local_active["knowledge"] == 0
        assert responses[0]["status"] == 413
        assert responses[-1]["body"] == b'{"detail":"document_file_too_large"}'

    asyncio.run(scenario())


def test_chunked_upload_without_content_length_is_bounded_during_streaming() -> None:
    """无 Content-Length 的知识库分块上传在流式拉取超限时立即中止并返回 413。"""

    async def scenario() -> None:
        completed_handlers = 0

        async def application(scope: object, receive: object, send: object) -> None:
            nonlocal completed_handlers
            del scope, send
            await _drain_request_body(receive)
            completed_handlers += 1

        middleware = UploadAdmissionMiddleware(application)
        messages = [
            {"type": "http.request", "body": b"x" * (11 * 1024 * 1024), "more_body": True},
            {"type": "http.request", "body": b"y" * (11 * 1024 * 1024), "more_body": True},
            {"type": "http.request", "body": b"z", "more_body": True},
            {"type": "http.request", "body": b"never-read", "more_body": False},
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
        assert completed_handlers == 0
        assert responses[0]["status"] == 413
        assert responses[-1]["body"] == b'{"detail":"document_file_too_large"}'
        # 超限分块触发异常后应立即停读，剩余分块不会被继续消费。
        assert len(messages) == 1

    asyncio.run(scenario())


def test_personal_markdown_chunked_upload_is_bounded_during_streaming() -> None:
    """个人 Markdown 在无 Content-Length 的流式拉取中超限返回稳定 413。"""

    async def scenario() -> None:
        completed_handlers = 0

        async def application(scope: object, receive: object, send: object) -> None:
            nonlocal completed_handlers
            del scope, send
            await _drain_request_body(receive)
            completed_handlers += 1

        middleware = UploadAdmissionMiddleware(application)
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
        assert completed_handlers == 0
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
def test_personal_json_chunked_write_is_bounded_during_streaming(path: str) -> None:
    """个人创建和发布 JSON 在无 Content-Length 的分块请求中同样受流式限流保护。"""

    async def scenario() -> None:
        completed_handlers = 0

        async def application(scope: object, receive: object, send: object) -> None:
            nonlocal completed_handlers
            del scope, send
            await _drain_request_body(receive)
            completed_handlers += 1

        middleware = UploadAdmissionMiddleware(application)
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
        assert completed_handlers == 0
        assert responses[0]["status"] == 413
        body = responses[-1]["body"]
        assert isinstance(body, bytes)
        assert b'"detail":"skill_personal_request_too_large"' in body

    asyncio.run(scenario())


def test_forged_small_content_length_is_still_bounded_by_streaming_counter() -> None:
    """即使伪造较小的 Content-Length，实际请求体超限时仍会被流式计数器拦截。"""

    async def scenario() -> None:
        completed_handlers = 0

        async def application(scope: object, receive: object, send: object) -> None:
            nonlocal completed_handlers
            del scope, send
            await _drain_request_body(receive)
            completed_handlers += 1

        middleware = UploadAdmissionMiddleware(application)
        messages = [
            {"type": "http.request", "body": b"a" * (1024 * 1024), "more_body": True},
            {"type": "http.request", "body": b"b" * (1024 * 1024 + 1), "more_body": False},
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
                "path": "/api/v1/me/skills",
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", b"16"),
                ],
            },
            receive,
            send,
        )
        assert completed_handlers == 0
        assert responses[0]["status"] == 413
        assert responses[-1]["body"] == b'{"detail":"skill_personal_request_too_large"}'

    asyncio.run(scenario())


def test_knowledge_upload_enforces_local_slot_limit_and_releases_after_downstream() -> None:
    """知识库上传使用进程内 4 槽位上限：满载立即返回 429 不读体，完成后立即归还槽位。"""

    async def scenario() -> None:
        release_uploads = asyncio.Event()
        entered = 0
        slots_full = asyncio.Event()

        async def application(scope: object, receive: object, send: object) -> None:
            nonlocal entered
            del scope, send
            payload = await _drain_request_body(receive)
            assert payload == b"ok"
            entered += 1
            if entered == 4:
                slots_full.set()
            await release_uploads.wait()

        middleware = UploadAdmissionMiddleware(application)

        async def ok_receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"ok", "more_body": False}

        async def noop_send(message: dict[str, object]) -> None:
            del message

        tasks = [
            asyncio.create_task(
                middleware(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": "/api/v1/knowledge-bases/kb_1/documents/upload",
                    },
                    ok_receive,
                    noop_send,
                )
            )
            for _ in range(4)
        ]
        await asyncio.wait_for(slots_full.wait(), timeout=2)
        assert middleware._local_active["knowledge"] == 4

        rejected_responses: list[dict[str, object]] = []

        async def fail_receive() -> dict[str, object]:
            raise AssertionError("request body must not be read without an upload slot")

        async def send_rejected(message: dict[str, object]) -> None:
            rejected_responses.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/knowledge-bases/kb_1/versions/upload",
            },
            fail_receive,
            send_rejected,
        )
        assert rejected_responses[0]["status"] == 429
        assert rejected_responses[-1]["body"] == b'{"detail":"knowledge_upload_capacity_exceeded"}'

        release_uploads.set()
        await asyncio.gather(*tasks)
        assert middleware._local_active["knowledge"] == 0

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

        middleware = UploadAdmissionMiddleware(application)

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


def test_upload_middleware_returns_kind_specific_timeout_error_code() -> None:
    """真实超时取消阻塞下游，按 upload_kind 返回准确 504 错误码并归还本地槽位。"""

    async def scenario() -> None:
        cancelled_paths: list[str] = []

        async def blocking_application(
            scope: dict[str, object],
            receive: object,
            send: object,
        ) -> None:
            del receive, send
            path = str(scope.get("path", ""))
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_paths.append(path)
                raise

        middleware = UploadAdmissionMiddleware(
            blocking_application,
            timeout_seconds=0.01,
        )

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
            ("/api/v1/me/skills", b'{"detail":"upload_timeout"}'),
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

        assert cancelled_paths == [path for path, _ in cases]
        assert all(active == 0 for active in middleware._local_active.values())

    asyncio.run(scenario())


def test_upload_middleware_does_not_swallow_downstream_or_post_response_timeout() -> None:
    """下游业务自身抛出 TimeoutError 或响应开始后超时均原样抛出，不被误报 504 或静默吞错。"""

    async def scenario() -> None:
        async def ok_receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"ok", "more_body": False}

        # 1. 下游在上传总超时尚未到期时自行抛出 TimeoutError -> 必须原样传播而非误报 504
        async def downstream_timeout_app(
            scope: object,
            receive: object,
            send: object,
        ) -> None:
            del scope, receive, send
            raise TimeoutError("upstream_rpc_timeout")

        middleware_long = UploadAdmissionMiddleware(
            downstream_timeout_app,
            timeout_seconds=5.0,
        )
        long_responses: list[dict[str, object]] = []

        async def send_long(message: dict[str, object]) -> None:
            long_responses.append(message)

        with pytest.raises(TimeoutError, match="upstream_rpc_timeout"):
            await middleware_long(
                {"type": "http", "method": "POST", "path": "/api/v1/me/skills"},
                ok_receive,
                send_long,
            )
        assert long_responses == []
        assert middleware_long._local_active["personal_json"] == 0

        # 2. 响应头已发送后触发真实上传超时 -> 必须重新抛出 TimeoutError 且归还槽位
        async def post_start_blocking_app(
            scope: object,
            receive: object,
            send: object,
        ) -> None:
            del scope, receive
            assert callable(send)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await asyncio.Event().wait()

        middleware_short = UploadAdmissionMiddleware(
            post_start_blocking_app,
            timeout_seconds=0.01,
        )
        short_responses: list[dict[str, object]] = []

        async def send_short(message: dict[str, object]) -> None:
            short_responses.append(message)

        with pytest.raises(TimeoutError):
            await middleware_short(
                {"type": "http", "method": "POST", "path": "/api/v1/me/skills"},
                ok_receive,
                send_short,
            )
        assert len(short_responses) == 1
        assert short_responses[0]["status"] == 200
        assert middleware_short._local_active["personal_json"] == 0

    asyncio.run(scenario())


def test_fastapi_e2e_multipart_and_chunked_uploads_return_413_without_hitting_route() -> None:
    """真实 FastAPI + TestClient 端到端验证 Content-Length 与 chunked 超限均返回 413。"""
    app = FastAPI()
    app.add_middleware(UploadAdmissionMiddleware)
    parse_calls = 0
    json_calls = 0

    @app.post("/api/v1/me/skills/parse")
    async def parse_endpoint(file: Annotated[UploadFile, File()]) -> dict[str, int]:
        nonlocal parse_calls
        parse_calls += 1
        data = await file.read()
        return {"size": len(data)}

    @app.post("/api/v1/me/skills")
    async def create_endpoint(payload: dict[str, Any]) -> dict[str, int]:
        nonlocal json_calls
        json_calls += 1
        return {"len": len(str(payload.get("guide", "")))}

    boundary = "athena-test-boundary"
    oversized_markdown = b"m" * (MAX_PERSONAL_MARKDOWN_BYTES + 64 * 1024 + 512)

    def _chunked_multipart_stream() -> Iterator[bytes]:
        yield (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="big.md"\r\n'
            f"Content-Type: text/markdown\r\n\r\n"
        ).encode("ascii")
        chunk_size = 128 * 1024
        for offset in range(0, len(oversized_markdown), chunk_size):
            yield oversized_markdown[offset : offset + chunk_size]
        yield f"\r\n--{boundary}--\r\n".encode("ascii")

    def _chunked_json_stream() -> Iterator[bytes]:
        yield b'{"guide":"'
        for _ in range(33):
            yield b"a" * (64 * 1024)
        yield b'"}'

    with TestClient(app) as client:
        # 1. 带 Content-Length 的超限 multipart 请求 -> 快拒 413，不进入路由
        content_length_resp = client.post(
            "/api/v1/me/skills/parse",
            files={"file": ("big.md", oversized_markdown, "text/markdown")},
        )
        assert content_length_resp.status_code == 413
        assert content_length_resp.json() == {"detail": "skill_markdown_too_large"}

        # 2. 无 Content-Length 的 chunked multipart 超限请求 -> Starlette 解析期中断并返回 413
        chunked_multipart_resp = client.post(
            "/api/v1/me/skills/parse",
            content=_chunked_multipart_stream(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        assert chunked_multipart_resp.status_code == 413
        assert chunked_multipart_resp.json() == {"detail": "skill_markdown_too_large"}

        # 3. 无 Content-Length 的 chunked JSON 超限请求 -> FastAPI 读体期中断并返回 413
        chunked_json_resp = client.post(
            "/api/v1/me/skills",
            content=_chunked_json_stream(),
            headers={"Content-Type": "application/json"},
        )
        assert chunked_json_resp.status_code == 413
        assert chunked_json_resp.json() == {"detail": "skill_personal_request_too_large"}

        assert parse_calls == 0
        assert json_calls == 0

        # 4. 正常大小上传在流式透传下可完整读取，同时证明先前 413 已释放本地槽位
        ok_multipart = client.post(
            "/api/v1/me/skills/parse",
            files={"file": ("ok.md", b"# title\nbody", "text/markdown")},
        )
        assert ok_multipart.status_code == 200
        assert ok_multipart.json() == {"size": len(b"# title\nbody")}

        ok_json = client.post(
            "/api/v1/me/skills",
            json={"guide": "hello"},
        )
        assert ok_json.status_code == 200
        assert ok_json.json() == {"len": 5}
        assert parse_calls == 1
        assert json_calls == 1


def test_staged_upload_offloads_disk_io_to_worker_thread_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """staged_upload 把创建、写块、关闭与删除下沉到工作线程，并在所有退出路径清理临时文件。"""
    import api.upload as upload_module

    worker_thread_ids: list[int] = []
    main_thread_id = threading.get_ident()
    real_write = upload_module._write_chunk
    real_cleanup = upload_module._cleanup_staged_file

    def tracked_write(state: Any, chunk: bytes) -> None:
        worker_thread_ids.append(threading.get_ident())
        real_write(state, chunk)

    def tracked_cleanup(state: Any) -> None:
        worker_thread_ids.append(threading.get_ident())
        real_cleanup(state)

    monkeypatch.setattr(upload_module, "_write_chunk", tracked_write)
    monkeypatch.setattr(upload_module, "_cleanup_staged_file", tracked_cleanup)

    async def scenario() -> None:
        # 1. 正常多块写入：内容一致且退出上下文后自动清理
        payload = b"0123456789abcdef" * 20_000  # > 128 KiB，确保触发多次分块写入
        captured_path: Path | None = None
        async with staged_upload(
            UploadFile(file=io.BytesIO(payload), filename="sample.bin"),
            maximum_bytes=len(payload),
        ) as path:
            captured_path = path
            assert path.exists()
            assert path.read_bytes() == payload
        assert captured_path is not None and not captured_path.exists()

        # 2. 超限抛出 UploadTooLargeError，不残留临时文件
        seen_paths: list[Path] = []
        real_create = upload_module._create_staged_file

        def capture_create(state: Any) -> Path | None:
            created_path = real_create(state)
            if created_path is not None:
                seen_paths.append(created_path)
            return created_path

        monkeypatch.setattr(upload_module, "_create_staged_file", capture_create)
        with pytest.raises(UploadTooLargeError, match="upload_too_large"):
            async with staged_upload(
                UploadFile(file=io.BytesIO(b"a" * 32), filename="big.bin"),
                maximum_bytes=16,
            ):
                raise AssertionError("should not yield on overflow")
        assert len(seen_paths) == 1 and not seen_paths[0].exists()

        # 3. 空文件抛出 ValueError("upload_empty")，不残留临时文件
        with pytest.raises(ValueError, match="upload_empty"):
            async with staged_upload(
                UploadFile(file=io.BytesIO(b""), filename="empty.bin"),
                maximum_bytes=16,
            ):
                raise AssertionError("should not yield on empty upload")
        assert len(seen_paths) == 2 and not seen_paths[1].exists()

    asyncio.run(scenario())
    assert len(worker_thread_ids) >= 3
    assert all(thread_id != main_thread_id for thread_id in worker_thread_ids)


def test_staged_upload_cancel_during_create_leaves_no_temp_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """在创建临时文件期间收到 CancelledError，工作线程在入锁发现已取消后必须同步关句柄并删文件。"""
    import tempfile as std_tempfile

    import api.upload as upload_module

    created_on_disk = threading.Event()
    release_create = threading.Event()
    create_finished = threading.Event()
    created_handles: list[Any] = []
    worker_errors: list[BaseException] = []
    real_named_temp = std_tempfile.NamedTemporaryFile
    real_create = upload_module._create_staged_file

    def slow_named_temp(*args: Any, **kwargs: Any) -> Any:
        kwargs["dir"] = tmp_path
        handle = real_named_temp(*args, **kwargs)
        created_handles.append(handle)
        # 模拟工作线程已创建临时文件，但在登记 state 前主协程被取消。
        created_on_disk.set()
        assert release_create.wait(timeout=5)
        return handle

    def tracked_create(state: Any) -> Path | None:
        try:
            return real_create(state)
        except BaseException as exc:
            worker_errors.append(exc)
            raise
        finally:
            create_finished.set()

    monkeypatch.setattr("api.upload.tempfile.NamedTemporaryFile", slow_named_temp)
    monkeypatch.setattr(upload_module, "_create_staged_file", tracked_create)

    async def scenario() -> None:
        async def run_staged() -> None:
            async with staged_upload(
                UploadFile(file=io.BytesIO(b"hello"), filename="demo.bin"),
                maximum_bytes=1024,
            ):
                raise AssertionError("cancelled upload must not yield")

        task = asyncio.create_task(run_staged())
        await asyncio.to_thread(created_on_disk.wait, 5)
        assert list(tmp_path.glob("athena-upload-*.bin"))

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # 放行仍在工作线程中的创建调用，验证其感知 cancelled 后自动关闭并删除临时文件。
        release_create.set()
        assert await asyncio.to_thread(create_finished.wait, 5)

    asyncio.run(scenario())
    assert worker_errors == []
    assert len(created_handles) == 1
    assert created_handles[0].closed
    assert list(tmp_path.glob("athena-upload-*.bin")) == []


def test_staged_upload_cancel_during_write_has_no_thread_race_and_leaves_no_temp_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """在分块写入期间触发 CancelledError，写入与关闭/清理按锁串行化，无 ValueError 且无文件残留。"""
    import tempfile as std_tempfile

    import api.upload as upload_module

    # 场景 1：取消发生在写入线程已持有锁执行 write() 期间 -> 清理线程等锁释放后再关并删
    write_in_progress = threading.Event()
    release_write = threading.Event()
    write_finished = threading.Event()
    worker_errors: list[BaseException] = []
    created_handles: list[Any] = []
    real_named_temp = std_tempfile.NamedTemporaryFile
    real_write_chunk = upload_module._write_chunk

    class _BlockingHandleProxy:
        """代理真实临时文件句柄，在首次 write() 内停顿以构造取消竞争窗口。"""

        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.name = inner.name

        @property
        def closed(self) -> bool:
            return bool(self._inner.closed)

        def write(self, data: bytes) -> int:
            write_in_progress.set()
            assert release_write.wait(timeout=5)
            return int(self._inner.write(data))

        def close(self) -> None:
            self._inner.close()

    def tracked_named_temp(*args: Any, **kwargs: Any) -> Any:
        kwargs["dir"] = tmp_path
        proxy = _BlockingHandleProxy(real_named_temp(*args, **kwargs))
        created_handles.append(proxy)
        return proxy

    def tracked_write_chunk(state: Any, chunk: bytes) -> None:
        try:
            real_write_chunk(state, chunk)
        except BaseException as exc:
            worker_errors.append(exc)
            raise
        finally:
            write_finished.set()

    monkeypatch.setattr("api.upload.tempfile.NamedTemporaryFile", tracked_named_temp)
    monkeypatch.setattr(upload_module, "_write_chunk", tracked_write_chunk)

    async def scenario_cancel_mid_write() -> None:
        async def run_staged() -> None:
            async with staged_upload(
                UploadFile(file=io.BytesIO(b"chunk-data"), filename="demo.bin"),
                maximum_bytes=1024,
            ):
                raise AssertionError("cancelled upload must not yield")

        task = asyncio.create_task(run_staged())
        assert await asyncio.to_thread(write_in_progress.wait, 5)
        task.cancel()
        # 让主协程进入 finally 并把清理任务投递到工作线程与写线程竞争同一把锁。
        await asyncio.sleep(0.02)
        release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(write_finished.wait, 5)

    asyncio.run(scenario_cancel_mid_write())
    assert worker_errors == []
    assert len(created_handles) == 1
    assert created_handles[0].closed
    assert list(tmp_path.glob("athena-upload-*.bin")) == []

    # 场景 2：取消发生并先完成清理后，滞后的写入线程再进入 _write_chunk -> 见已关闭直接跳过无报错
    late_write_entered = threading.Event()
    release_late_write = threading.Event()
    late_write_finished = threading.Event()
    late_errors: list[BaseException] = []
    late_handles: list[Any] = []

    def plain_named_temp(*args: Any, **kwargs: Any) -> Any:
        kwargs["dir"] = tmp_path
        handle = real_named_temp(*args, **kwargs)
        late_handles.append(handle)
        return handle

    def delayed_write_chunk(state: Any, chunk: bytes) -> None:
        try:
            late_write_entered.set()
            assert release_late_write.wait(timeout=5)
            real_write_chunk(state, chunk)
        except BaseException as exc:
            late_errors.append(exc)
            raise
        finally:
            late_write_finished.set()

    monkeypatch.setattr("api.upload.tempfile.NamedTemporaryFile", plain_named_temp)
    monkeypatch.setattr(upload_module, "_write_chunk", delayed_write_chunk)

    async def scenario_cancel_before_write_lock() -> None:
        async def run_staged() -> None:
            async with staged_upload(
                UploadFile(file=io.BytesIO(b"late-chunk"), filename="demo2.bin"),
                maximum_bytes=1024,
            ):
                raise AssertionError("cancelled upload must not yield")

        task = asyncio.create_task(run_staged())
        assert await asyncio.to_thread(late_write_entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # 此时清理已完成且文件已删除，再放行滞后的写入线程验证不会触发 ValueError。
        release_late_write.set()
        assert await asyncio.to_thread(late_write_finished.wait, 5)

    asyncio.run(scenario_cancel_before_write_lock())
    assert late_errors == []
    assert len(late_handles) == 1
    assert late_handles[0].closed
    assert list(tmp_path.glob("athena-upload-*.bin")) == []
