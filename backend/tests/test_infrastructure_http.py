"""共享 HTTP 基础设施单元测试：字节限流、URL 校验、有界客户端、退避重试与 MinIO 连接池。"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import httpx2
import pytest

from infrastructure.http import (
    DEFAULT_KEEPALIVE_EXPIRY_SECONDS,
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_MAX_KEEPALIVE_CONNECTIONS,
    BoundedResponseStream,
    BoundedResponseTransport,
    ResponseLimitError,
    RetryableHttpError,
    build_bounded_async_client,
    build_http_timeout,
    caused_by_response_limit,
    compute_retry_delay,
    find_response_limit_error,
    has_control_characters,
    parse_retry_after_seconds,
    run_with_http_retry,
    validate_http_base_url,
)
from infrastructure.storage import build_minio_http_pool


class _CustomLimitError(ConnectionError):
    """测试 error_factory 自定义异常类型。"""


class _TrackedStream(httpx2.AsyncByteStream):
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
    def __init__(
        self,
        stream: _TrackedStream,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.stream = stream
        self.headers = headers or {}
        self.seen_requests: list[httpx2.Request] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.seen_requests.append(request)
        return httpx2.Response(200, headers=self.headers, stream=self.stream, request=request)

    async def aclose(self) -> None:
        return None


def test_bounded_stream_and_transport_support_custom_codes_and_error_factory() -> None:
    """支持调用方定制错误码、自定义异常工厂以及可选编码检查开关。"""
    stream = _TrackedStream([b"hello", b" world"])
    bounded = BoundedResponseStream(
        stream,
        limit=8,
        is_sse=False,
        too_large_code="custom_too_large",
        error_factory=_CustomLimitError,
    )

    async def consume(target: httpx2.AsyncByteStream) -> bytes:
        return b"".join([chunk async for chunk in target])

    with pytest.raises(_CustomLimitError, match="custom_too_large"):
        asyncio.run(consume(bounded))

    sse_stream = BoundedResponseStream(
        _TrackedStream([b"data: 1234567890\n\n"]),
        limit=100,
        is_sse=True,
        event_limit=10,
        event_too_large_code="custom_event_too_large",
        error_factory=_CustomLimitError,
    )
    with pytest.raises(_CustomLimitError, match="custom_event_too_large"):
        asyncio.run(consume(sse_stream))

    # 验证 transport 级别自定义错误码与 error_factory（含负值 Content-Length）。
    for headers, expected_code in (
        ({"content-encoding": "gzip"}, "custom_encoding"),
        ({"content-length": "not-int"}, "custom_len_invalid"),
        ({"content-length": "-5"}, "custom_len_invalid"),
        ({"content-length": "999"}, "custom_too_large"),
    ):
        inner_stream = _TrackedStream([b"unused"])
        transport = BoundedResponseTransport(
            _StubTransport(inner_stream, headers=headers),
            limit=100,
            too_large_code="custom_too_large",
            encoding_unsupported_code="custom_encoding",
            length_invalid_code="custom_len_invalid",
            error_factory=_CustomLimitError,
        )
        with pytest.raises(_CustomLimitError, match=expected_code):
            asyncio.run(
                transport.handle_async_request(httpx2.Request("GET", "https://example.test/data"))
            )
        assert inner_stream.closed is True
        assert inner_stream.iterated is False

    # 关闭非 identity 编码拒绝开关时，transport 放行 gzip 响应头并仍包装有界流。
    gzip_stream = _TrackedStream([b"raw"])
    permissive = BoundedResponseTransport(
        _StubTransport(gzip_stream, headers={"content-encoding": "gzip"}),
        limit=100,
        reject_non_identity_encoding=False,
    )

    async def fetch_permissive() -> bytes:
        resp = await permissive.handle_async_request(
            httpx2.Request("GET", "https://example.test/data")
        )
        try:
            return b"".join([chunk async for chunk in resp.aiter_raw()])
        finally:
            await resp.aclose()

    assert asyncio.run(fetch_permissive()) == b"raw"
    assert gzip_stream.closed is True

    with pytest.raises(ValueError, match="response_limit_invalid"):
        BoundedResponseStream(_TrackedStream([]), 0)
    with pytest.raises(ValueError, match="response_event_limit_invalid"):
        BoundedResponseStream(_TrackedStream([]), 10, True, event_limit=0)
    with pytest.raises(ValueError, match="response_limit_invalid"):
        BoundedResponseTransport(_StubTransport(_TrackedStream([])), 0)


def test_find_and_caused_by_response_limit_handles_chains_and_cycles() -> None:
    limit_error = ResponseLimitError("custom_limit")
    wrapper = RuntimeError("outer")
    wrapper.__cause__ = limit_error
    assert find_response_limit_error(wrapper) is limit_error
    assert caused_by_response_limit(wrapper) is True

    first = RuntimeError("a")
    second = RuntimeError("b")
    first.__cause__ = second
    second.__context__ = first
    assert find_response_limit_error(first) is None
    assert caused_by_response_limit(first) is False


def test_has_control_characters_and_validate_http_base_url() -> None:
    assert has_control_characters("safe-value_123") is False
    assert has_control_characters("bad\nline") is True
    assert has_control_characters("bad\x00null") is True
    assert has_control_characters("bad\x7fdel") is True

    for valid_url in (
        "https://api.example.test/v1",
        "https://api.example.test/v1/",
        "http://127.0.0.1:8000/v1",
        "http://[::1]:8000/v1",
        "https://[2001:db8::1]/v1",
        "http://embed:8000/v1",
        "HTTPS://Embed.Example.Test:443/v1",
    ):
        validate_http_base_url(valid_url)

    # allow_http=False 拒绝明文 HTTP。
    validate_http_base_url("https://secure.example.test/v1", allow_http=False)
    with pytest.raises(ValueError, match="http_base_url_invalid"):
        validate_http_base_url("http://insecure.example.test/v1", allow_http=False)

    # allow_query 控制是否允许查询参数，但始终拒绝 fragment。
    validate_http_base_url("https://api.example.test/v1?api-version=2025", allow_query=True)
    with pytest.raises(ValueError, match="http_base_url_invalid"):
        validate_http_base_url("https://api.example.test/v1?api-version=2025", allow_query=False)
    with pytest.raises(ValueError, match="http_base_url_invalid"):
        validate_http_base_url("https://api.example.test/v1?", allow_query=False)
    with pytest.raises(ValueError, match="http_base_url_invalid"):
        validate_http_base_url("https://api.example.test/v1?a=1#frag", allow_query=True)

    for bad_url in (
        "",
        "   ",
        "ftp://example.test/v1",
        "example.test/v1",
        "http://user:pass@example.test/v1",
        "https://user@example.test/v1",
        "https://example.test/v1#frag",
        "https://example.test/v1#",
        "http://example.test:0/v1",
        "http://example.test:65536/v1",
        "http://example.test:abc/v1",
        "http://example.test:/v1",
        "http://[::1]:/v1",
        "http://[::1]extra/v1",
        "http://[fe80::1%eth0]:8000/v1",
        "http://127.0.0.999:8000/v1",
        "http://bad_host.test/v1",
        "http://-bad.test/v1",
        "http://bad..test/v1",
        "http://example.test/v1\r\nX-Inject: 1",
        "http://example test/v1",
        "http://example.test\\v1",
    ):
        with pytest.raises(ValueError, match="http_base_url_invalid"):
            validate_http_base_url(bad_url)


def test_build_bounded_async_client_defaults_headers_limits_and_transport_wrapping() -> None:
    timeout = build_http_timeout(30.0)
    assert timeout.connect == 10.0
    assert timeout.read == 30.0
    short_timeout = build_http_timeout(3.5)
    assert short_timeout.connect == 3.5
    assert short_timeout.read == 3.5

    with pytest.raises(ValueError, match="http_timeout_invalid"):
        build_http_timeout(0.0)
    with pytest.raises(ValueError, match="http_timeout_invalid"):
        build_http_timeout(float("nan"))

    async def scenario() -> None:
        inner_stream = _TrackedStream([b"1234567890"])
        stub = _StubTransport(inner_stream)
        client = build_bounded_async_client(
            timeout_seconds=12.0,
            max_response_bytes=5,
            transport=stub,
            headers={"Accept": "application/json"},
            too_large_code="client_too_large",
        )
        try:
            assert client.follow_redirects is False
            assert client.timeout.connect == 10.0
            assert client.timeout.read == 12.0
            assert client.headers["accept-encoding"] == "identity"
            assert client.headers["accept"] == "application/json"
            assert isinstance(client._transport, BoundedResponseTransport)
            with pytest.raises(ResponseLimitError, match="client_too_large"):
                await client.get("https://example.test/items")
            assert stub.seen_requests[0].headers["accept-encoding"] == "identity"
        finally:
            await client.aclose()

        # 未传入自定义 transport 但开启 max_response_bytes 时，内层默认传输也应承载连接池限制。
        default_bounded = build_bounded_async_client(
            timeout_seconds=15.0,
            max_response_bytes=1024,
            trust_env=False,
        )
        try:
            assert isinstance(default_bounded._transport, BoundedResponseTransport)
            inner = default_bounded._transport._inner
            assert isinstance(inner, httpx2.AsyncHTTPTransport)
            assert inner._pool._max_connections == DEFAULT_MAX_CONNECTIONS
            assert inner._pool._max_keepalive_connections == DEFAULT_MAX_KEEPALIVE_CONNECTIONS
            assert inner._pool._keepalive_expiry == DEFAULT_KEEPALIVE_EXPIRY_SECONDS
        finally:
            await default_bounded.aclose()

        # 调用方显式指定 accept-encoding 时不被默认 identity 覆盖。
        custom_enc_client = build_bounded_async_client(headers={"Accept-Encoding": "gzip"})
        try:
            assert custom_enc_client.headers["accept-encoding"] == "gzip"
            assert custom_enc_client.follow_redirects is False
        finally:
            await custom_enc_client.aclose()

        # 公共受限客户端不允许通过参数开启重定向。
        with pytest.raises(TypeError):
            cast(Any, build_bounded_async_client)(follow_redirects=True)

    asyncio.run(scenario())


def test_parse_retry_after_and_compute_retry_delay() -> None:
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert parse_retry_after_seconds(None, now=now) is None
    assert parse_retry_after_seconds("", now=now) is None
    assert parse_retry_after_seconds("   ", now=now) is None
    assert parse_retry_after_seconds("12.5", now=now) == 12.5
    assert parse_retry_after_seconds("0", now=now) == 0.0
    assert parse_retry_after_seconds("999999", now=now) == 86_400.0
    assert parse_retry_after_seconds("999999", now=now, maximum=5.0) == 5.0
    assert parse_retry_after_seconds("-1", now=now) is None
    assert parse_retry_after_seconds("nan", now=now) is None
    assert parse_retry_after_seconds("inf", now=now) is None
    assert parse_retry_after_seconds("not-a-date", now=now) is None
    assert parse_retry_after_seconds("Thu, 01 Jan 2026 00:00:30 GMT", now=now) == 30.0
    assert parse_retry_after_seconds("Wed, 31 Dec 2025 23:59:50 GMT", now=now) is None

    assert compute_retry_delay(0) == 0.25
    assert compute_retry_delay(1) == 0.5
    assert compute_retry_delay(2) == 1.0
    assert compute_retry_delay(10) == 5.0
    # 极大 attempt_index 必须安全截断至 max_seconds，绝不抛出 OverflowError。
    assert compute_retry_delay(2000) == 5.0
    assert compute_retry_delay(10**20) == 5.0
    assert compute_retry_delay(2000, base_seconds=0.0) == 0.0
    assert compute_retry_delay(0, retry_after_header="2.5") == 2.5
    assert compute_retry_delay(0, retry_after_header="100") == 5.0
    assert compute_retry_delay(1, retry_after_header="0.1") == 0.5
    assert compute_retry_delay(0, retry_after_header="invalid") == 0.25
    assert (
        compute_retry_delay(
            0,
            retry_after_header="Thu, 01 Jan 2026 00:00:03 GMT",
            now=now,
        )
        == 3.0
    )


def test_run_with_http_retry_backoff_retry_after_exhaustion_and_cancellation() -> None:
    """验证 tenacity 异步重试执行器的退避、Retry-After、非瞬态透传与取消透传。"""

    async def scenario() -> None:
        # 1) 结合 Retry-After 与默认指数退避并在第三次成功。
        sleeps: list[float] = []
        callbacks: list[tuple[int, float, int | None]] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def on_retry(attempt_number: int, delay: float, error: RetryableHttpError) -> None:
            callbacks.append((attempt_number, delay, error.status_code))

        async def flaky(attempt_index: int) -> str:
            if attempt_index == 0:
                raise RetryableHttpError(
                    retry_after_header="1.5",
                    final_error=RuntimeError("rate_limited"),
                    status_code=429,
                )
            if attempt_index == 1:
                raise RetryableHttpError(
                    retry_after_header=None,
                    final_error=RuntimeError("unavailable"),
                    status_code=503,
                )
            return "ok"

        result = await run_with_http_retry(
            flaky,
            max_retries=2,
            sleep=fake_sleep,
            on_retry=on_retry,
        )
        assert result == "ok"
        assert sleeps == [1.5, 0.5]
        assert callbacks == [(1, 1.5, 429), (2, 0.5, 503)]

        # 2) 重试耗尽后抛出 final_error 且截断异常链。
        sleeps.clear()

        async def always_retryable(_attempt_index: int) -> str:
            raise RetryableHttpError(
                final_error=RuntimeError("exhausted_code"),
                status_code=503,
            )

        with pytest.raises(RuntimeError, match="exhausted_code") as exc_info:
            await run_with_http_retry(always_retryable, max_retries=2, sleep=fake_sleep)
        assert exc_info.value.__cause__ is None
        assert sleeps == [0.25, 0.5]

        # 3) max_retries=0 时仅尝试一次且不进入 sleep。
        sleeps.clear()
        zero_attempts = 0

        async def fail_once(_attempt_index: int) -> str:
            nonlocal zero_attempts
            zero_attempts += 1
            raise RetryableHttpError(final_error=RuntimeError("no_retry"))

        with pytest.raises(RuntimeError, match="no_retry"):
            await run_with_http_retry(fail_once, max_retries=0, sleep=fake_sleep)
        assert zero_attempts == 1
        assert sleeps == []

        # 4) 非 RetryableHttpError 直接透传且不重试。
        sleeps.clear()
        non_retry_attempts = 0

        async def non_retryable(_attempt_index: int) -> str:
            nonlocal non_retry_attempts
            non_retry_attempts += 1
            raise ValueError("bad_request")

        with pytest.raises(ValueError, match="bad_request"):
            await run_with_http_retry(non_retryable, max_retries=2, sleep=fake_sleep)
        assert non_retry_attempts == 1
        assert sleeps == []

        # 5) asyncio.CancelledError 绝不被捕获或重试，直接传播。
        sleeps.clear()
        cancel_attempts = 0

        async def cancelled_op(_attempt_index: int) -> str:
            nonlocal cancel_attempts
            cancel_attempts += 1
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await run_with_http_retry(cancelled_op, max_retries=2, sleep=fake_sleep)
        assert cancel_attempts == 1
        assert sleeps == []

        # 6) 入口提前校验非法 base_seconds / max_seconds，不调用业务协程。
        called = False

        async def should_not_run(_attempt_index: int) -> str:
            nonlocal called
            called = True
            return "unexpected"

        for bad_base in (-0.1, float("nan"), float("inf")):
            with pytest.raises(ValueError, match="retry_base_seconds_invalid"):
                await run_with_http_retry(should_not_run, max_retries=0, base_seconds=bad_base)
        for bad_max in (-1.0, float("nan"), float("inf")):
            with pytest.raises(ValueError, match="retry_max_seconds_invalid"):
                await run_with_http_retry(should_not_run, max_retries=0, max_seconds=bad_max)
        assert called is False

    asyncio.run(scenario())


def test_build_minio_http_pool_validates_arguments_and_configures_pool() -> None:
    pool = build_minio_http_pool(maxsize=6, block=False, secure=False, connect_timeout=3.0)
    try:
        assert pool.connection_pool_kw["maxsize"] == 6
        assert pool.connection_pool_kw["block"] is False
        assert pool.connection_pool_kw["timeout"].connect_timeout == 3.0
        assert pool.connection_pool_kw["timeout"].read_timeout == 30.0
        retries = pool.connection_pool_kw["retries"]
        assert retries.total == 2
        assert retries.connect == 2
        assert retries.read == 2
        assert retries.status == 2
        assert retries.redirect == 0
        assert retries.other == 0
        assert retries.backoff_factor == 0.2
        assert retries.respect_retry_after_header is False
        assert retries.status_forcelist == (500, 502, 503, 504)
        assert retries.allowed_methods == frozenset({"HEAD", "GET", "PUT", "DELETE"})
        assert "ssl_context" not in pool.connection_pool_kw
    finally:
        pool.clear()

    secure_pool = build_minio_http_pool(maxsize=4, block=True, secure=True)
    try:
        assert secure_pool.connection_pool_kw["maxsize"] == 4
        assert secure_pool.connection_pool_kw["block"] is True
        context = secure_pool.connection_pool_kw["ssl_context"]
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
    finally:
        secure_pool.clear()

    with pytest.raises(ValueError, match="minio_http_pool_maxsize_invalid"):
        build_minio_http_pool(maxsize=0, block=True, secure=False)
    with pytest.raises(ValueError, match="minio_http_pool_connect_timeout_invalid"):
        build_minio_http_pool(maxsize=4, block=True, secure=False, connect_timeout=0.0)
    with pytest.raises(ValueError, match="minio_http_pool_read_timeout_invalid"):
        build_minio_http_pool(maxsize=4, block=True, secure=False, read_timeout=-1.0)
