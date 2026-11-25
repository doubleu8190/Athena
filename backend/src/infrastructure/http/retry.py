"""通用 HTTP Retry-After 解析、指数退避计算与基于 tenacity 的异步重试执行器。"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
)

DEFAULT_RETRY_AFTER_MAX_SECONDS = 86_400.0
DEFAULT_BACKOFF_BASE_SECONDS = 0.25
DEFAULT_BACKOFF_MAX_SECONDS = 5.0


class RetryableHttpError(Exception):
    """供异步重试执行器识别的瞬态 HTTP 错误，可选携带 Retry-After 与耗尽后终态异常。"""

    def __init__(
        self,
        *,
        retry_after_header: str | None = None,
        final_error: Exception | None = None,
        status_code: int | None = None,
    ) -> None:
        self.retry_after_header = retry_after_header
        self.final_error = final_error
        self.status_code = status_code
        super().__init__("retryable_http_error")


def parse_retry_after_seconds(
    value: str | None,
    *,
    now: datetime | None = None,
    maximum: float = DEFAULT_RETRY_AFTER_MAX_SECONDS,
) -> float | None:
    """解析 Retry-After 秒数或 HTTP 日期；负值、非有限数或非法格式返回 None。"""
    if value is None:
        return None
    if not math.isfinite(maximum) or maximum < 0:
        raise ValueError("retry_after_maximum_invalid")
    stripped = value.strip()
    if not stripped:
        return None
    try:
        seconds = float(stripped)
    except ValueError:
        try:
            retry_time = parsedate_to_datetime(stripped)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_time.tzinfo is None:
            retry_time = retry_time.replace(tzinfo=UTC)
        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)
        seconds = (retry_time.astimezone(UTC) - current_time.astimezone(UTC)).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, maximum)


def compute_retry_delay(
    attempt_index: int,
    *,
    base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
    retry_after_header: str | None = None,
    now: datetime | None = None,
) -> float:
    """按 0 起始尝试序号计算有界指数退避时长，并尊重合法且受上限约束的 Retry-After。"""
    if attempt_index < 0:
        raise ValueError("retry_attempt_index_invalid")
    if not math.isfinite(base_seconds) or base_seconds < 0:
        raise ValueError("retry_base_seconds_invalid")
    if not math.isfinite(max_seconds) or max_seconds < 0:
        raise ValueError("retry_max_seconds_invalid")
    # 使用 math.ldexp 在 O(1) 空间内计算指数退避，防止极大 attempt_index 触发大整数或溢出错误。
    if base_seconds == 0.0 or max_seconds == 0.0:
        delay = 0.0
    else:
        try:
            delay = min(float(max_seconds), math.ldexp(base_seconds, attempt_index))
        except OverflowError:
            delay = float(max_seconds)
    parsed = parse_retry_after_seconds(retry_after_header, now=now, maximum=max_seconds)
    if parsed is not None:
        delay = min(max_seconds, max(delay, parsed))
    return delay


async def run_with_http_retry[ResultT](
    operation: Callable[[int], Awaitable[ResultT]],
    *,
    max_retries: int,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
    on_retry: Callable[[int, float, RetryableHttpError], None] | None = None,
) -> ResultT:
    """基于 tenacity.AsyncRetrying 执行有界异步退避重试，绝不捕获 asyncio.CancelledError。"""
    if max_retries < 0:
        raise ValueError("retry_max_retries_invalid")
    # 在首次执行前提前校验退避参数，避免仅在真正触发重试时才暴露非法配置。
    if not math.isfinite(base_seconds) or base_seconds < 0:
        raise ValueError("retry_base_seconds_invalid")
    if not math.isfinite(max_seconds) or max_seconds < 0:
        raise ValueError("retry_max_seconds_invalid")

    def _compute_wait(retry_state: RetryCallState) -> float:
        retry_after_header: str | None = None
        if retry_state.outcome is not None and retry_state.outcome.failed:
            exception = retry_state.outcome.exception()
            if isinstance(exception, RetryableHttpError):
                retry_after_header = exception.retry_after_header
        # tenacity attempt_number 从 1 开始，换算为 0 起始序号以对齐 compute_retry_delay。
        attempt_index = max(0, retry_state.attempt_number - 1)
        return compute_retry_delay(
            attempt_index,
            base_seconds=base_seconds,
            max_seconds=max_seconds,
            retry_after_header=retry_after_header,
        )

    def _before_sleep(retry_state: RetryCallState) -> None:
        if on_retry is None or retry_state.outcome is None or not retry_state.outcome.failed:
            return
        exception = retry_state.outcome.exception()
        if not isinstance(exception, RetryableHttpError):
            return
        delay = float(retry_state.next_action.sleep) if retry_state.next_action is not None else 0.0
        on_retry(retry_state.attempt_number, delay, exception)

    retrying = AsyncRetrying(
        sleep=sleep,
        stop=stop_after_attempt(max_retries + 1),
        wait=_compute_wait,
        retry=retry_if_exception_type(RetryableHttpError),
        before_sleep=_before_sleep,
        reraise=True,
    )

    try:
        async for attempt in retrying:
            attempt_index = attempt.retry_state.attempt_number - 1
            # 在 with attempt 之外执行业务协程，仅把显式 RetryableHttpError 交由 tenacity 调度；
            # 非重试异常与 asyncio.CancelledError 均不被捕获，直接原样透传。
            try:
                return await operation(attempt_index)
            except RetryableHttpError as error:
                with attempt:
                    raise error
    except RetryableHttpError as error:
        if error.final_error is not None:
            raise error.final_error from None
        raise
    raise RuntimeError("http_retry_exhausted")


__all__ = [
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_BACKOFF_MAX_SECONDS",
    "DEFAULT_RETRY_AFTER_MAX_SECONDS",
    "RetryableHttpError",
    "compute_retry_delay",
    "parse_retry_after_seconds",
    "run_with_http_retry",
]
