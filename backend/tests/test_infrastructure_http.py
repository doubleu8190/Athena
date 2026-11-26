"""共享 HTTP 基础设施单元测试：URL 校验、退避重试与 MinIO 连接池。"""

from __future__ import annotations

import asyncio
import ssl
from datetime import UTC, datetime

import pytest

from infrastructure.http import (
    RetryableHttpError,
    compute_retry_delay,
    has_control_characters,
    parse_retry_after_seconds,
    run_with_http_retry,
    validate_http_base_url,
)
from infrastructure.storage import build_minio_http_pool


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
