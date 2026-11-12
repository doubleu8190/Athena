"""OpenAI 兼容 HTTP 响应的原始字节限流工具。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import NoReturn, cast

import httpx2

logger = logging.getLogger(__name__)


class ResponseLimitError(Exception):
    """HTTP 响应 body 超过配置的原始字节上限。"""


def caused_by_response_limit(error: BaseException) -> bool:
    """检查异常因果链中是否包含响应限流错误，并防御循环引用。"""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if isinstance(current, ResponseLimitError):
            return True
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return False


class BoundedResponseStream(httpx2.AsyncByteStream):
    """在读取 transport 字节时限制原始 body 累计大小和 SSE 事件大小。"""

    def __init__(
        self,
        stream: httpx2.AsyncByteStream,
        limit: int,
        is_sse: bool,
        event_limit: int | None = None,
    ) -> None:
        self._stream = stream
        self._limit = limit
        self._event_limit = event_limit or limit
        self._is_sse = is_sse
        self._total = 0
        self._event_bytes = 0
        self._line_bytes = 0
        self._pending_cr = False

    def _scan_sse(self, chunk: bytes) -> None:
        # 逐字节扫描，保证 LF、CRLF、CR 不受网络分片边界影响。
        for byte in chunk:
            self._event_bytes += 1
            if self._event_bytes > self._event_limit:
                raise ResponseLimitError("llm_stream_event_too_large")
            if self._pending_cr:
                self._pending_cr = False
                if byte == 0x0A:
                    continue
            if byte == 0x0D:
                self._finish_sse_line()
                self._pending_cr = True
            elif byte == 0x0A:
                self._finish_sse_line()
            else:
                self._line_bytes += 1

    def _finish_sse_line(self) -> None:
        if self._line_bytes == 0:
            self._event_bytes = 0
        self._line_bytes = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            self._total += len(chunk)
            if self._total > self._limit:
                raise ResponseLimitError("llm_response_too_large")
            if self._is_sse:
                self._scan_sse(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class BoundedResponseTransport(httpx2.AsyncBaseTransport):
    """为 HTTP transport 响应包装按客户端配置的原始字节上限。"""

    def __init__(
        self,
        inner: httpx2.AsyncBaseTransport,
        limit: int,
        *,
        event_limit: int | None = None,
    ) -> None:
        self._inner = inner
        self._limit = limit
        self._event_limit = event_limit

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self._inner.handle_async_request(request)
        content_encoding = response.headers.get("content-encoding", "").strip().lower()
        # 非 identity 编码无法在不读取解码流的情况下确认原始大小，直接关闭并拒绝。
        if content_encoding not in {"", "identity"}:
            await _close_before_limit(
                response,
                "llm_response_encoding_unsupported",
            )
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                parsed_length = int(content_length)
            except ValueError:
                await _close_before_limit(response, "llm_response_length_invalid")
            # 声明的长度已经超限时不必读取 body，先关闭上游响应。
            if parsed_length > self._limit:
                await _close_before_limit(response, "llm_response_too_large")
        is_sse = "text/event-stream" in response.headers.get("content-type", "").lower()
        # 即使未声明 content-length，也在实际读取时执行累计和 SSE 事件限制。
        response.stream = BoundedResponseStream(
            cast(httpx2.AsyncByteStream, response.stream),
            self._limit,
            is_sse,
            self._event_limit,
        )
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


async def _close_before_limit(
    response: httpx2.Response,
    error_code: str,
) -> NoReturn:
    """在限流拒绝前尽力关闭响应，清理失败不得覆盖稳定限流错误。"""
    try:
        await response.aclose()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("llm_response_close_failed code=llm_cleanup_failed")
    raise ResponseLimitError(error_code) from None


__all__ = [
    "BoundedResponseStream",
    "BoundedResponseTransport",
    "ResponseLimitError",
    "caused_by_response_limit",
]
