"""LLM 调用重试策略 — 指数退避 + 错误分类 + 多级故障转移.

核心组件:
- ErrorCategory: 错误语义化分类（TRANSIENT / RATE_LIMITED / CONTEXT_OVERFLOW /
  MODEL_MISBEHAVIOR / PERMANENT / UNKNOWN）
- retry_with_backoff(): 通用指数退避重试函数
- LLMRetryManager: 封装多级故障转移逻辑的管理器

重试参数默认值:
- max_attempts=3, min_delay_ms=2000, max_delay_ms=30000, jitter=0.1
- timeout_ms=60000

多级故障转移策略（LLMRetryManager）:
1. 同模型重试（指数退避）
2. 换模型（遍历 fallback_providers，仅可恢复错误类别触发）
3. 返回最终失败结果
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
    """LLM 调用错误分类.

    根据错误的可恢复性进行语义化分类，决定重试策略：
    - TRANSIENT / RATE_LIMITED / MODEL_MISBEHAVIOR: 可重试
    - CONTEXT_OVERFLOW / PERMANENT: 不可重试，快速失败
    """

    TRANSIENT = "transient"
    """网络超时、连接重置等瞬态错误，可重试。"""

    RATE_LIMITED = "rate_limited"
    """API 限流 (429)，需冷却等待后重试。"""

    CONTEXT_OVERFLOW = "context_overflow"
    """上下文超长，需压缩上下文而非重试。"""

    MODEL_MISBEHAVIOR = "model_misbehavior"
    """模型返回无效格式，可重试或切换模型。"""

    PERMANENT = "permanent"
    """无效 API Key、计费问题等永久错误，不可恢复。"""

    UNKNOWN = "unknown"
    """未分类错误，默认按可重试处理。"""


@dataclass
class RetryConfig:
    """重试配置.

    控制指数退避重试的行为参数。延迟计算公式:
        delay = min(min_delay_ms * 2^attempt, max_delay_ms) + jitter

    属性：
        max_attempts: 最大重试次数（含首次调用）。
        min_delay_ms: 首次重试的最小延迟（毫秒）。
        max_delay_ms: 延迟上限（毫秒），防止指数退避无限增长。
        jitter: 随机抖动系数 (0~1)，避免重试风暴。实际抖动 = delay * jitter * random()。
        timeout_ms: 整个重试流程的总超时时间（毫秒）。
    """

    max_attempts: int = 3
    min_delay_ms: int = 2000
    max_delay_ms: int = 30000
    jitter: float = 0.1
    timeout_ms: int = 60000


# 各错误类别的默认重试配置；None 表示该类别不可重试
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
    """对异常进行语义化分类，决定重试策略.

    分类优先级：TRANSIENT > RATE_LIMITED > CONTEXT_OVERFLOW >
    MODEL_MISBEHAVIOR > PERMANENT > UNKNOWN。

    参数：
        error: 待分类的异常实例。

    返回值：
        错误类别枚举值。
    """
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
    """从 HTTP 错误中提取 retry-after 头，用于覆盖默认退避延迟.

    支持两种 header 格式:
    - ``retry-after``: 秒数（标准 HTTP header）
    - ``retry-after-ms``: 毫秒数（部分 API 扩展）

    参数：
        error: HTTP 相关异常，需具有 ``response.headers`` 属性。

    返回值：
        建议等待秒数（上限 60s），无法提取时返回 None。
    """
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
    """重试结果.

    属性：
        success: 是否最终成功。
        result: 成功时的 LLM 响应消息。
        error: 失败时的最后一次异常。
        attempts: 实际尝试次数（含首次调用）。
        total_duration_ms: 整个重试流程的总耗时（毫秒）。
        error_category: 最后一次失败的错误类别。
    """

    success: bool
    result: BaseMessage | None = None
    error: Exception | None = None
    attempts: int = 0
    total_duration_ms: float = 0
    error_category: ErrorCategory = ErrorCategory.UNKNOWN


async def retry_with_backoff(
    func: Callable[..., Awaitable[Any]],
    *args: Any,
    retry_config: RetryConfig | None = None,
    error_category: ErrorCategory | None = None,
    on_retry: Callable[[int, Exception, float], Awaitable[None]] | None = None,
    **kwargs: Any,
) -> RetryResult:
    """执行异步函数，失败时使用指数退避重试.

    延迟计算: ``delay = min(min_delay_ms * 2^attempt, max_delay_ms) + jitter``。
    若异常携带 HTTP retry-after header，优先使用该值。
    不可重试的错误类别（CONTEXT_OVERFLOW / PERMANENT）立即返回失败。

    参数：
        func: 待执行的异步函数。
        *args: 传递给 func 的位置参数。
        retry_config: 重试配置。未提供时根据 error_category 从 RETRY_CONFIGS 查找；
            若也无对应配置，默认 max_attempts=1（不重试）。
        error_category: 错误类别，用于查找默认重试配置。
        on_retry: 每次重试前的回调 ``(attempt, error, delay_s) -> None``。
            未提供时使用默认 logger.warning 回调。
        **kwargs: 传递给 func 的关键字参数。

    返回值：
        RetryResult，包含成功/失败状态、结果或异常、尝试次数和总耗时。
    """
    if retry_config is None and error_category is not None:
        retry_config = RETRY_CONFIGS.get(error_category)
    if retry_config is None:
        retry_config = RetryConfig(max_attempts=1)

    if on_retry is None:
        async def _default_on_retry(
            attempt: int, error: Exception, delay_s: float
        ) -> None:
            """执行“default on retry”操作。

            参数：
                attempt (int): 输入参数；其类型和取值约束由方法签名及实现定义。
                error (Exception): 输入参数；其类型和取值约束由方法签名及实现定义。
                delay_s (float): 输入参数；其类型和取值约束由方法签名及实现定义。

            返回值：
                None: 操作结果；具体语义由调用场景决定。

            异常：
                Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
            """
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

            # 不可重试的错误类别（CONTEXT_OVERFLOW / PERMANENT）立即返回
            if RETRY_CONFIGS.get(category) is None:
                total_duration = (time.time() * 1000) - start_time
                return RetryResult(
                    success=False,
                    error=e,
                    attempts=attempts,
                    total_duration_ms=total_duration,
                    error_category=category,
                )

            # 总超时检查：防止重试循环耗时超过 timeout_ms
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

            # 计算退避延迟：优先使用 HTTP retry-after header，否则指数退避 + jitter
            if attempt < retry_config.max_attempts - 1:
                retry_after = get_retry_after(e)
                if retry_after:
                    delay_s = retry_after
                else:
                    # 指数退避: base = min_delay * 2^attempt，上限 max_delay
                    base_delay_ms = retry_config.min_delay_ms * (2**attempt)
                    delay_ms = min(base_delay_ms, retry_config.max_delay_ms)
                    # 随机抖动防止多客户端同时重试形成"惊群效应"
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
    """LLM 重试管理器 — 封装重试与多级故障转移逻辑供 LLMProvider 使用.

    故障转移策略:
    1. 同模型重试（指数退避）
    2. 换模型（遍历 fallback_providers，仅 TRANSIENT / RATE_LIMITED /
       MODEL_MISBEHAVIOR 触发）
    3. 返回最终失败结果
    """

    def __init__(self, retry_config: RetryConfig | None = None) -> None:
        """初始化重试管理器.

        参数：
            retry_config: 重试配置，未提供时使用默认值。
        """
        self._config = retry_config or RetryConfig()
        self._fallback_providers: list[LLMProvider] = []

    def add_fallback_provider(self, provider: LLMProvider) -> None:
        """添加备用 LLM Provider（故障转移用）.

        参数：
            provider: 备用 LLM Provider 实例，按添加顺序依次尝试。
        """
        self._fallback_providers.append(provider)

    @property
    def config(self) -> RetryConfig:
        """返回当前 Provider 使用的重试策略."""
        return self._config

    async def execute_with_retry(
        self,
        func: Callable[..., Awaitable[Any]],
        *args: Any,
        on_retry: Callable[[int, Exception, float], Awaitable[None]] | None = None,
        **kwargs: Any,
    ) -> RetryResult:
        """执行 LLM 调用，失败时按多级故障转移策略重试.

        参数：
            func: 待执行的 LLM 调用异步函数。
            *args: 传递给 func 的位置参数。
            on_retry: 每次重试前的回调，参见 ``retry_with_backoff``。
            **kwargs: 传递给 func 的关键字参数。

        返回值：
            RetryResult，包含成功/失败状态、结果或异常、尝试次数和总耗时。
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
                    fallback_result = await fallback.ainvoke(*args, **kwargs)
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
