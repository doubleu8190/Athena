"""表驱动上传准入与流式请求体限流中间件。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from starlette.exceptions import HTTPException

from domain.skill import MAX_PERSONAL_MARKDOWN_BYTES

_PERSONAL_MARKDOWN_MULTIPART_OVERHEAD = 64 * 1024
_PERSONAL_JSON_MAXIMUM = 2 * 1024 * 1024
_DEFAULT_UPLOAD_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True, slots=True)
class UploadPolicy:
    """单类上传入口的请求体大小、进程内并发槽位与超时准入策略。"""

    kind: str
    maximum_bytes: int
    overflow_code: str
    local_slot_limit: int
    capacity_error_code: str
    timeout_error_code: str


UPLOAD_POLICIES: Mapping[str, UploadPolicy] = {
    "knowledge": UploadPolicy(
        kind="knowledge",
        maximum_bytes=22 * 1024 * 1024,  # 20 MiB 文档体加有界 multipart 元数据
        overflow_code="document_file_too_large",
        local_slot_limit=4,
        capacity_error_code="knowledge_upload_capacity_exceeded",
        timeout_error_code="knowledge_upload_timeout",
    ),
    "skill": UploadPolicy(
        kind="skill",
        maximum_bytes=26 * 1024 * 1024,
        overflow_code="skill_zip_too_large",
        local_slot_limit=2,
        capacity_error_code="skill_upload_capacity_exceeded",
        timeout_error_code="skill_upload_timeout",
    ),
    "session_file": UploadPolicy(
        kind="session_file",
        maximum_bytes=52 * 1024 * 1024,
        overflow_code="document_file_too_large",
        local_slot_limit=4,
        capacity_error_code="session_file_upload_capacity_exceeded",
        timeout_error_code="session_file_upload_timeout",
    ),
    "personal_markdown": UploadPolicy(
        kind="personal_markdown",
        maximum_bytes=MAX_PERSONAL_MARKDOWN_BYTES + _PERSONAL_MARKDOWN_MULTIPART_OVERHEAD,
        overflow_code="skill_markdown_too_large",
        local_slot_limit=4,
        capacity_error_code="upload_capacity_exceeded",
        timeout_error_code="upload_timeout",
    ),
    "personal_json": UploadPolicy(
        kind="personal_json",
        maximum_bytes=_PERSONAL_JSON_MAXIMUM,
        overflow_code="skill_personal_request_too_large",
        local_slot_limit=8,
        capacity_error_code="upload_capacity_exceeded",
        timeout_error_code="upload_timeout",
    ),
}


def _match_upload_kind(method: str, path: str) -> str | None:
    """将 HTTP 方法和路径映射到上传策略类别；非上传写入口返回 None。"""
    if method.upper() != "POST":
        return None
    if path.startswith("/api/v1/knowledge-bases/") and (
        path.endswith("/documents/upload") or path.endswith("/versions/upload")
    ):
        return "knowledge"
    if path == "/api/v1/admin/skills/import" or (
        path.startswith("/api/v1/admin/skills/") and path.endswith("/versions")
    ):
        return "skill"
    if path.startswith("/api/v1/sessions/") and path.endswith("/files"):
        return "session_file"
    if path in {"/api/v1/me/skills/parse", "/api/v1/admin/skills/parse-markdown"}:
        return "personal_markdown"
    if (
        path in {"/api/v1/me/skills", "/api/v1/admin/skills/markdown"}
        or (path.startswith("/api/v1/me/skills/") and path.endswith("/versions"))
        or (path.startswith("/api/v1/admin/skills/") and path.endswith("/versions/markdown"))
    ):
        return "personal_json"
    return None


def match_upload_policy(
    method: str,
    path: str,
    *,
    policies: Mapping[str, UploadPolicy] = UPLOAD_POLICIES,
) -> UploadPolicy | None:
    """根据请求方法与路径查表返回对应的上传准入策略。"""
    kind = _match_upload_kind(method, path)
    if kind is None:
        return None
    return policies.get(kind)


class _UploadBodyTooLargeError(HTTPException):
    """流式累加请求体超过上传上限时抛出的内部哨兵异常。

    继承 Starlette HTTPException（直接派生自 Exception，非 ValueError/OSError）：
    - 避免被业务路由的 `except ValueError` / `except OSError` 误捕；
    - 在 FastAPI body/multipart 解析阶段可穿透 `get_request_handler`（不被转成 400），
      并由 Starlette ExceptionMiddleware 稳定输出 413 JSON；
    - 在无 ExceptionMiddleware 的裸 ASGI 下游中会冒泡回本中间件并输出 413 JSON。
    """

    def __init__(self, code: str) -> None:
        super().__init__(status_code=413, detail=code)
        self.code = code


def _parse_content_length(scope: Mapping[str, object]) -> int | None:
    """解析请求头中的合法非负 Content-Length；多头时取较大值，非法或缺失返回 None。"""
    raw_headers = scope.get("headers")
    if not isinstance(raw_headers, Iterable):
        return None
    largest: int | None = None
    for item in raw_headers:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        name, value = item
        if not isinstance(name, bytes) or not isinstance(value, bytes):
            continue
        if name.lower() != b"content-length":
            continue
        try:
            parsed = int(value.decode("latin-1").strip())
        except ValueError:
            continue
        if parsed >= 0 and (largest is None or parsed > largest):
            largest = parsed
    return largest


async def _send_json(send: object, status: int, detail: str) -> None:
    """发送上传准入或大小限制 JSON 错误响应。"""
    assert callable(send)
    payload = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class UploadAdmissionMiddleware:
    """在 multipart/JSON 解析阶段提供上传准入、进程内并发槽位隔离、超时与流式体积限流。"""

    def __init__(
        self,
        app: object,
        *,
        policies: Mapping[str, UploadPolicy] | None = None,
        timeout_seconds: float = _DEFAULT_UPLOAD_TIMEOUT_SECONDS,
    ) -> None:
        self.app = app
        self._policies: Mapping[str, UploadPolicy] = (
            policies if policies is not None else UPLOAD_POLICIES
        )
        self._timeout_seconds = timeout_seconds
        # 按策略 kind 独立维护进程内并发占用计数；单线程事件循环中同步增减，实现零排队快速失败。
        self._local_active: dict[str, int] = dict.fromkeys(self._policies, 0)

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        scope_type = scope.get("type")
        if scope_type is not None and scope_type != "http":
            await self.app(scope, receive, send)  # type: ignore[operator]
            return

        path = str(scope.get("path", ""))
        method = str(scope.get("method", ""))
        policy = match_upload_policy(method, path, policies=self._policies)
        if policy is None:
            await self.app(scope, receive, send)  # type: ignore[operator]
            return

        assert callable(receive) and callable(send)

        # 快速拒绝：显式声明的 Content-Length 超限时直接 413，不占用槽位、不读请求体。
        content_length = _parse_content_length(scope)
        if content_length is not None and content_length > policy.maximum_bytes:
            await _send_json(send, 413, policy.overflow_code)
            return

        # 并发准入：所有上传类型统一按策略 kind 使用独立进程内槽位，满载立即返回 429。
        if self._local_active[policy.kind] >= policy.local_slot_limit:
            await _send_json(send, 429, policy.capacity_error_code)
            return
        self._local_active[policy.kind] += 1

        response_started = False
        received_bytes = 0

        async def tracked_send(message: dict[str, object]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        async def bounded_receive() -> dict[str, object]:
            """流式透传 ASGI receive 并在累加字节超限时立即中断，避免整包预缓冲落盘。"""
            nonlocal received_bytes
            message = await receive()
            if not isinstance(message, dict):
                raise TypeError("invalid_asgi_receive_message")
            if message.get("type") == "http.request":
                chunk = message.get("body", b"")
                if isinstance(chunk, (bytes, bytearray, memoryview)):
                    received_bytes += len(chunk)
                if received_bytes > policy.maximum_bytes:
                    raise _UploadBodyTooLargeError(policy.overflow_code)
            return message

        timeout_cm: asyncio.Timeout | None = None
        try:
            async with asyncio.timeout(self._timeout_seconds) as timeout_cm:
                await self.app(scope, bounded_receive, tracked_send)  # type: ignore[operator]
        except _UploadBodyTooLargeError as error:
            if not response_started:
                await _send_json(tracked_send, 413, error.code)
            else:
                raise
        except TimeoutError:
            # 仅当确由本中间件上传总超时触发且尚未开始发送响应时映射为 504；
            # 下游自身业务 TimeoutError 或响应已开始后的超时须原样抛出，避免误报或静默吞错。
            if timeout_cm is None or not timeout_cm.expired() or response_started:
                raise
            await _send_json(tracked_send, 504, policy.timeout_error_code)
        finally:
            # 无论下游成功、超限、超时或取消，均确保立即归还进程内上传槽位。
            self._local_active[policy.kind] -= 1


__all__ = [
    "UPLOAD_POLICIES",
    "UploadAdmissionMiddleware",
    "UploadPolicy",
    "match_upload_policy",
]
