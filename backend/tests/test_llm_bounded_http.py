"""共享 LLM HTTP 响应限流工具测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

import httpx2
import pytest

from adapter.llm.bounded_http import (
    BoundedResponseStream,
    BoundedResponseTransport,
    ResponseLimitError,
    caused_by_response_limit,
)


class _TrackedStream(httpx2.AsyncByteStream):
    """记录底层流是否被读取或关闭。"""

    def __init__(self, chunks: Sequence[bytes]) -> None:
        self._chunks = chunks
        self.iterated = False
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.iterated = True
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _StubTransport(httpx2.AsyncBaseTransport):
    """返回预设响应流，供限流 transport 测试使用。"""

    def __init__(
        self,
        stream: _TrackedStream,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.stream = stream
        self.headers = headers or {}

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers=self.headers,
            stream=self.stream,
            request=request,
        )

    async def aclose(self) -> None:
        return None


class _FailingCloseStream(_TrackedStream):
    """模拟上游响应清理失败。"""

    async def aclose(self) -> None:
        self.closed = True
        raise RuntimeError("close failed")


async def _consume(stream: httpx2.AsyncByteStream) -> bytes:
    """读取并拼接限流流，确保异常在读取阶段触发。"""
    return b"".join([chunk async for chunk in stream])


async def _request(
    transport: BoundedResponseTransport,
) -> httpx2.Response:
    """通过限流 transport 发起不依赖网络的请求。"""
    return await transport.handle_async_request(
        httpx2.Request("GET", "https://llm.example.test/v1/models")
    )


async def _raw_body(response: httpx2.Response) -> bytes:
    """读取响应原始字节，避免测试侧额外解压。"""
    try:
        return b"".join([chunk async for chunk in response.aiter_raw()])
    finally:
        await response.aclose()


def test_bounded_stream_enforces_cumulative_body_limit() -> None:
    """累计原始 body 恰好达到上限时通过，超过时在下一次读取前失败。"""
    assert (
        asyncio.run(_consume(BoundedResponseStream(_TrackedStream([b"abc", b"de"]), 5, False)))
        == b"abcde"
    )
    with pytest.raises(ResponseLimitError, match="llm_response_too_large"):
        asyncio.run(_consume(BoundedResponseStream(_TrackedStream([b"abc", b"def"]), 5, False)))


def test_bounded_stream_rejects_oversize_sse_event() -> None:
    """SSE 单事件上限独立于累计 body 上限生效。"""
    payload = b"data: " + b"x" * 10 + b"\n\n"
    with pytest.raises(ResponseLimitError, match="llm_stream_event_too_large"):
        asyncio.run(
            _consume(
                BoundedResponseStream(
                    _TrackedStream([payload]),
                    len(payload) * 2,
                    True,
                    event_limit=len(payload) - 1,
                )
            )
        )


@pytest.mark.parametrize(
    ("line_ending", "event_limit"),
    ((b"\n", 9), (b"\r\n", 11), (b"\r", 9)),
)
def test_sse_line_endings_and_arbitrary_chunk_boundaries(
    line_ending: bytes, event_limit: int
) -> None:
    """LF/CRLF/CR 在任意切分下按空行结束事件并重置单事件计数。"""
    event = b"data: a" + line_ending + line_ending
    payload = event + event
    assert len(event) == event_limit

    for split in range(1, len(payload)):
        stream = BoundedResponseStream(
            _TrackedStream([payload[:split], payload[split:]]),
            len(payload),
            True,
            event_limit=event_limit,
        )
        assert asyncio.run(_consume(stream)) == payload

    stream = BoundedResponseStream(
        _TrackedStream([bytes([byte]) for byte in payload]),
        len(payload),
        True,
        event_limit=event_limit,
    )
    assert asyncio.run(_consume(stream)) == payload


def test_transport_rejects_gzip_without_reading_and_closes_response() -> None:
    """编码不被接受时在读取 body 前关闭响应。"""
    stream = _TrackedStream([b"compressed"])
    transport = BoundedResponseTransport(
        _StubTransport(stream, headers={"content-encoding": "gzip"}),
        1024,
    )

    with pytest.raises(ResponseLimitError, match="llm_response_encoding_unsupported"):
        asyncio.run(_request(transport))

    assert stream.closed is True
    assert stream.iterated is False


@pytest.mark.parametrize("content_encoding", (None, "", "identity", " Identity "))
def test_transport_accepts_identity_or_empty_content_encoding(
    content_encoding: str | None,
) -> None:
    """空值和 identity 编码被接受，响应仍经过字节限流包装。"""
    headers = {} if content_encoding is None else {"content-encoding": content_encoding}
    stream = _TrackedStream([b"plain", b" body"])
    transport = BoundedResponseTransport(_StubTransport(stream, headers=headers), 1024)

    async def run() -> bytes:
        response = await _request(transport)
        return await _raw_body(response)

    assert asyncio.run(run()) == b"plain body"
    assert stream.closed is True


@pytest.mark.parametrize(
    ("content_length", "error_code"),
    (("1025", "llm_response_too_large"), ("not-a-number", "llm_response_length_invalid")),
)
def test_transport_rejects_oversize_or_invalid_content_length_without_reading(
    content_length: str,
    error_code: str,
) -> None:
    """content-length 超限或非法时拒绝响应并关闭底层流。"""
    stream = _TrackedStream([b"unused"])
    transport = BoundedResponseTransport(
        _StubTransport(stream, headers={"content-length": content_length}),
        1024,
    )

    with pytest.raises(ResponseLimitError, match=error_code):
        asyncio.run(_request(transport))

    assert stream.closed is True
    assert stream.iterated is False


@pytest.mark.parametrize(
    ("headers", "error_code"),
    (
        (
            {"content-encoding": "gzip"},
            "llm_response_encoding_unsupported",
        ),
        (
            {"content-length": "1025"},
            "llm_response_too_large",
        ),
        (
            {"content-length": "not-a-number"},
            "llm_response_length_invalid",
        ),
    ),
)
def test_transport_close_failure_does_not_mask_response_limit_error(
    headers: dict[str, str],
    error_code: str,
) -> None:
    """限流拒绝先保留稳定错误，再尽力关闭响应。"""
    stream = _FailingCloseStream([b"unused"])
    transport = BoundedResponseTransport(
        _StubTransport(stream, headers=headers),
        1024,
    )

    with pytest.raises(ResponseLimitError, match=error_code):
        asyncio.run(_request(transport))

    assert stream.closed is True
    assert stream.iterated is False


def test_transport_applies_sse_event_limit_from_content_type() -> None:
    """transport 根据 text/event-stream 为包装流启用单事件限制。"""
    payload = b"data: " + b"x" * 20 + b"\n\n"
    stream = _TrackedStream([payload])
    transport = BoundedResponseTransport(
        _StubTransport(stream, headers={"content-type": "text/event-stream; charset=utf-8"}),
        len(payload) * 2,
        event_limit=len(payload) - 1,
    )

    async def run() -> None:
        response = await _request(transport)
        with pytest.raises(ResponseLimitError, match="llm_stream_event_too_large"):
            await _raw_body(response)

    asyncio.run(run())
    assert stream.iterated is True
    assert stream.closed is True


def test_caused_by_response_limit_walks_cause_and_context_without_cycles() -> None:
    """异常链同时检查 cause/context，并终止于循环引用。"""
    limit_error = ResponseLimitError("llm_response_too_large")

    cause_wrapper = RuntimeError("outer")
    middle = ValueError("middle")
    middle.__cause__ = limit_error
    cause_wrapper.__cause__ = middle
    assert caused_by_response_limit(cause_wrapper) is True

    context_wrapper = RuntimeError("outer")
    context_wrapper.__context__ = limit_error
    assert caused_by_response_limit(context_wrapper) is True

    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__cause__ = second
    second.__context__ = first
    assert caused_by_response_limit(first) is False
    assert caused_by_response_limit(limit_error) is True
