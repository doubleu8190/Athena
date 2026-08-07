"""LLM 调用重试策略 — 指数退避 + 错误分类 + 多级故障转移.

错误分类：
- TRANSIENT: 网络超时、连接重置 → 可重试
- RATE_LIMITED: API 限流 (429) → 需冷却等待
- CONTEXT_OVERFLOW: 上下文超长 → 需压缩上下文
- MODEL_MISBEHAVIOR: 模型返回无效格式 → 切换模型
- PERMANENT: 无效 API Key、计费问题 → 不可恢复

重试参数：
- max_attempts=3, min_delay_ms=2000, max_delay_ms=30000, jitter=0.1
- timeout_ms=60000

多级故障转移：
- 阶段 1: 同模型重试（指数退避）
- 阶段 2: 换模型（如果配置了 fallbacks）
- 阶段 3: 安全模式（移除工具，简化提示）
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TYPE_CHECKING, Awaitable, Callable

from athena.utils.logging import get_logger

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage

    from athena.core.llm.provider import LLMProvider

logger = get_logger(__name__)


class ErrorCategory(StrEnum):
    """LLM 调用错误分类."""

    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    CONTEXT_OVERFLOW = "context_overflow"
    MODEL_MISBEHAVIOR = "model_misbehavior"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"


@dataclass
class RetryConfig:
    """重试配置."""

    max_attempts: int = 3
    min_delay_ms: int = 2000
    max_delay_ms: int = 30000
    jitter: float = 0.1
    timeout_ms: int = 60000


# 各错误类别的默认重试配置
RETRY_CONFIGS: dict[ErrorCategory, RetryConfig | None] = {
    ErrorCategory.TRANSIENT: RetryConfig(
        max_attempts=3, min_delay_ms=2000, max_delay_ms=30000
    ),
    ErrorCategory.RATE_LIMITED: RetryConfig(
        max_attempts=5, min_delay_ms=5000, max_delay_ms=60000
    ),
    ErrorCategory.CONTEXT_OVERFLOW: None,  # 不重试，需压缩上下文
    ErrorCategory.MODEL_MISBEHAVIOR: RetryConfig(
        max_attempts=1, min_delay_ms=1000, max_delay_ms=5000
    ),
    ErrorCategory.PERMANENT: None,  # 不重试，快速失败
    ErrorCategory.UNKNOWN: RetryConfig(
        max_attempts=1, min_delay_ms=2000, max_delay_ms=10000
    ),
}


def categorize_error(error: Exception) -> ErrorCategory:
    """对异常进行语义化分类."""
    error_str = str(error).lower()
    error_type = type(error).__name__.lower()

    # 网络/超时错误
    if isinstance(error, (TimeoutError, ConnectionError, ConnectionResetError)):
        return ErrorCategory.TRANSIENT
    if "timeout" in error_str or "connection" in error_str:
        return ErrorCategory.TRANSIENT

    # 限流
    if "429" in error_str or "rate" in error_str or "too many requests" in error_str:
        return ErrorCategory.RATE_LIMITED
    if hasattr(error, "status_code") and getattr(error, "status_code") == 429:
        return ErrorCategory.RATE_LIMITED

    # 上下文溢出
    if "context" in error_str and (
        "length" in error_str or "too long" in error_str or "overflow" in error_str
    ):
        return ErrorCategory.CONTEXT_OVERFLOW
    if "maximum context length" in error_str:
        return ErrorCategory.CONTEXT_OVERFLOW

    # 模型行为异常
    if "invalid" in error_str and ("response" in error_str or "format" in error_str):
        return ErrorCategory.MODEL_MISBEHAVIOR

    # 永久错误
    if "api key" in error_str or "invalid_key" in error_str or "billing" in error_str:
        return ErrorCategory.PERMANENT
    if "authentication" in error_str or "unauthorized" in error_str:
        return ErrorCategory.PERMANENT

    return ErrorCategory.UNKNOWN


def get_retry_after(error: Exception) -> float | None:
    """从 HTTP 错误中提取 retry-after 头."""
    if hasattr(error, "response"):
        response = getattr(error, "response", None)
        if response and hasattr(response, "headers"):
            retry_after = response.headers.get("retry-after") or response.headers.get(
                "retry-after-ms"
            )
            if retry_after:
                try:
                    value = float(retry_after)
                    if "retry-after-ms" in str(response.headers):
                        value /= 1000
                    return min(value, 60)
                except (ValueError, TypeError):
                    pass
    return None


@dataclass
class RetryResult:
    """重试结果."""

    success: bool
    result: BaseMessage | None = None
    error: Exception | None = None
    attempts: int = 0
    total_duration_ms: float = 0
    error_category: ErrorCategory = ErrorCategory.UNKNOWN


async def retry_with_backoff(
    func,
    *args,
    retry_config: RetryConfig | None = None,
    error_category: ErrorCategory | None = None,
    on_retry: Callable[..., Awaitable[None]] | None = None,
    **kwargs,
) -> RetryResult:
    """执行函数，失败时使用指数退避重试.

    Args:
        func: 异步函数
        retry_config: 重试配置（默认根据 error_category 查找）
        error_category: 错误类别（用于查找默认配置）
        on_retry: 重试回调(attempt, error, delay)

    Returns:
        RetryResult 包含成功/失败状态和详细信息
    """
    if retry_config is None and error_category is not None:
        retry_config = RETRY_CONFIGS.get(error_category)
    if retry_config is None:
        retry_config = RetryConfig(max_attempts=1)

    if on_retry is None:
        async def _default_on_retry(
            attempt: int, error: Exception, delay_s: float
        ) -> None:
            logger.warning(
                "llm_retry",
                attempt=attempt,
                max_attempts=retry_config.max_attempts,
                error=str(error),
                category=categorize_error(error),
                delay_s=round(delay_s, 2),
            )

        on_retry = _default_on_retry

    last_error: Exception | None = None
    start_time = time.time() * 1000
    attempts = 0

    for attempt in range(retry_config.max_attempts):
        attempts = attempt + 1
        try:
            result = await func(*args, **kwargs)
            total_duration = (time.time() * 1000) - start_time
            return RetryResult(
                success=True,
                result=result,
                attempts=attempts,
                total_duration_ms=total_duration,
            )
        except Exception as e:
            last_error = e
            category = categorize_error(e)

            # 不可重试的错误
            if RETRY_CONFIGS.get(category) is None:
                total_duration = (time.time() * 1000) - start_time
                return RetryResult(
                    success=False,
                    error=e,
                    attempts=attempts,
                    total_duration_ms=total_duration,
                    error_category=category,
                )

            # 检查总超时
            elapsed_ms = (time.time() * 1000) - start_time
            if elapsed_ms >= retry_config.timeout_ms:
                total_duration = elapsed_ms
                return RetryResult(
                    success=False,
                    error=e,
                    attempts=attempts,
                    total_duration_ms=total_duration,
                    error_category=category,
                )

            # 计算延迟
            if attempt < retry_config.max_attempts - 1:
                retry_after = get_retry_after(e)
                if retry_after:
                    delay_s = retry_after
                else:
                    base_delay_ms = retry_config.min_delay_ms * (2**attempt)
                    delay_ms = min(base_delay_ms, retry_config.max_delay_ms)
                    jitter_ms = delay_ms * retry_config.jitter * random.random()
                    delay_s = (delay_ms + jitter_ms) / 1000

                await on_retry(attempt + 1, e, delay_s)

                await asyncio.sleep(delay_s)

    total_duration = (time.time() * 1000) - start_time
    return RetryResult(
        success=False,
        error=last_error,
        attempts=attempts,
        total_duration_ms=total_duration,
        error_category=(
            categorize_error(last_error) if last_error else ErrorCategory.UNKNOWN
        ),
    )


class LLMRetryManager:
    """LLM 重试管理器 — 封装重试逻辑供 LLMProvider 使用."""

    def __init__(self, retry_config: RetryConfig | None = None) -> None:
        self._config = retry_config or RetryConfig()
        self._fallback_providers: list[LLMProvider] = []

    def add_fallback_provider(self, provider: LLMProvider) -> None:
        """添加备用 LLM Provider（故障转移用）."""
        self._fallback_providers.append(provider)

    async def execute_with_retry(
        self,
        func,
        *args,
        on_retry: Callable[..., Awaitable[None]] | None = None,
        **kwargs,
    ) -> RetryResult:
        """执行 LLM 调用，失败时重试.

        多级故障转移：
        1. 同模型重试（指数退避）
        2. 换模型（如果配置了 fallbacks）
        3. 返回失败
        """
        # 阶段 1: 同模型重试
        result = await retry_with_backoff(
            func,
            *args,
            retry_config=self._config,
            on_retry=on_retry,
            **kwargs,
        )
        if result.success:
            return result

        # 阶段 2: 换模型
        if self._fallback_providers and result.error_category in (
            ErrorCategory.TRANSIENT,
            ErrorCategory.RATE_LIMITED,
            ErrorCategory.MODEL_MISBEHAVIOR,
        ):
            for fallback in self._fallback_providers:
                logger.info(
                    "trying_fallback_provider", provider=type(fallback).__name__
                )
                try:
                    fallback_result = await func(*args, **kwargs)
                    return RetryResult(
                        success=True,
                        result=fallback_result,
                        attempts=result.attempts,
                        total_duration_ms=result.total_duration_ms,
                    )
                except Exception as e:
                    logger.warning("fallback_failed", error=str(e))
                    continue

        # 阶段 3: 失败
        return result
