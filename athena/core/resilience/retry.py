"""Retry executor with configurable exponential backoff.

Supports selective retry based on error type classification,
jitter to avoid thundering herd, and structured attempt logging.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from athena.logging_config import get_logger
from athena.api.metrics import athena_tool_retry_total, athena_tool_retry_success_total

logger = get_logger(__name__)

T = TypeVar("T")


class CircuitOpenError(Exception):
    """Raised when the circuit breaker rejects a request."""

    def __init__(self, tool_name: str, circuit_state: str) -> None:
        self.tool_name = tool_name
        self.circuit_state = circuit_state
        super().__init__(f"Circuit breaker is {circuit_state} for tool '{tool_name}'")


@dataclass
class AttemptRecord:
    """Record of a single retry attempt."""

    attempt: int
    timestamp: float
    success: bool
    duration_ms: float
    error_type: str | None = None
    error_message: str | None = None


@dataclass
class RetryResult:
    """Result of a retry execution."""

    success: bool
    result: Any = None
    error: Exception | None = None
    attempts: list[AttemptRecord] = field(default_factory=list)
    total_attempts: int = 0


@dataclass
class RetryConfig:
    """Configuration for the retry executor."""

    max_retries: int = 5
    initial_interval_ms: int = 200
    max_interval_ms: int = 30000
    backoff_multiplier: float = 2.0
    jitter: bool = True
    retryable_errors: list[str] = field(
        default_factory=lambda: [
            "TimeoutError",
            "ConnectionError",
            "ServiceUnavailable",
            "RateLimitExceeded",
            "InternalServerError",
        ]
    )
    non_retryable_errors: list[str] = field(
        default_factory=lambda: [
            "AuthenticationError",
            "InvalidParameters",
            "ToolNotFound",
        ]
    )


# Error classification keywords
_TIMEOUT_KEYWORDS = ("timeout",)
_CONNECTION_KEYWORDS = ("connection", "connect")
_RATE_LIMIT_KEYWORDS = ("rate limit", "429")
_UNAVAILABLE_KEYWORDS = ("503", "unavailable")
_SERVER_ERROR_KEYWORDS = ("500",)
_AUTH_KEYWORDS = ("auth", "401", "403")
_NOT_FOUND_KEYWORDS = ("not found", "404")


class RetryExecutor:
    """Configurable exponential-backoff retry engine."""

    def __init__(self, config: RetryConfig) -> None:
        self.config = config

    def is_retryable(self, error: Exception) -> bool:
        """Determine whether an error is eligible for retry.

        Non-retryable blacklist takes priority over retryable whitelist.
        """
        error_type = type(error).__name__

        if error_type in self.config.non_retryable_errors:
            return False
        if error_type in self.config.retryable_errors:
            return True

        # Fallback: keyword matching on error message
        error_msg = str(error).lower()
        return any(
            kw in error_msg
            for kw in ("timeout", "connection", "unavailable", "rate limit", "503", "429")
        )

    def classify_error(self, error: Exception) -> str:
        """Return a structured error type label."""
        error_type = type(error).__name__
        error_msg = str(error).lower()

        if any(kw in error_msg for kw in _TIMEOUT_KEYWORDS) or error_type == "TimeoutError":
            return "TimeoutError"
        if any(kw in error_msg for kw in _CONNECTION_KEYWORDS) or error_type == "ConnectionError":
            return "ConnectionError"
        if any(kw in error_msg for kw in _RATE_LIMIT_KEYWORDS):
            return "RateLimitExceeded"
        if any(kw in error_msg for kw in _UNAVAILABLE_KEYWORDS):
            return "ServiceUnavailable"
        if any(kw in error_msg for kw in _SERVER_ERROR_KEYWORDS):
            return "InternalServerError"
        if any(kw in error_msg for kw in _AUTH_KEYWORDS):
            return "AuthenticationError"
        if any(kw in error_msg for kw in _NOT_FOUND_KEYWORDS):
            return "ToolNotFound"
        return error_type

    def compute_delay(self, attempt: int) -> float:
        """Compute the backoff delay for a given attempt number (0-indexed).

        ``delay = min(initial_interval * multiplier^attempt, max_interval)``
        With optional jitter of ±25%.
        """
        base = self.config.initial_interval_ms / 1000.0
        delay = base * (self.config.backoff_multiplier**attempt)
        if self.config.jitter:
            delay *= 0.75 + random.random() * 0.5  # noqa: S311
        delay = min(delay, self.config.max_interval_ms / 1000.0)
        return delay

    async def execute(
        self,
        func: Callable[..., Awaitable[Any]],
        *args: Any,
        tool_name: str = "",
        circuit_breaker: Any | None = None,
        **kwargs: Any,
    ) -> RetryResult:
        """Execute *func* with retries.

        Args:
            func: The async callable to execute.
            *args: Positional arguments forwarded to *func*.
            tool_name: Tool name for logging.
            circuit_breaker: Optional ``CircuitBreaker`` instance consulted
                before each attempt.
            **kwargs: Keyword arguments forwarded to *func*.

        Returns:
            A ``RetryResult`` with success/failure status and attempt history.
        """
        # Late import to avoid circular dependency at module level
        from athena.core.resilience.circuit_breaker import CircuitBreaker

        attempts: list[AttemptRecord] = []
        last_error: Exception | None = None

        for attempt in range(self.config.max_retries + 1):
            # Circuit-breaker gate
            if circuit_breaker is not None:
                cb: CircuitBreaker = circuit_breaker
                if not await cb.allow_request():
                    logger.warning(
                        "retry_blocked_by_circuit",
                        tool=tool_name,
                        attempt=attempt,
                        circuit_state=cb.state.value,
                    )
                    raise CircuitOpenError(tool_name, cb.state.value)

            attempt_start = time.monotonic()
            try:
                result = await func(*args, **kwargs)
                duration_ms = (time.monotonic() - attempt_start) * 1000

                record = AttemptRecord(
                    attempt=attempt,
                    timestamp=time.time(),
                    success=True,
                    duration_ms=round(duration_ms, 2),
                )
                attempts.append(record)

                if circuit_breaker is not None:
                    cb: CircuitBreaker = circuit_breaker
                    await cb.record_result(success=True)

                logger.info(
                    "tool_retry_success",
                    tool=tool_name,
                    attempt=attempt,
                    duration_ms=round(duration_ms, 2),
                )

                if attempt > 0:
                    athena_tool_retry_success_total.labels(tool_name=tool_name).inc()

                return RetryResult(
                    success=True,
                    result=result,
                    attempts=attempts,
                    total_attempts=attempt + 1,
                )

            except CircuitOpenError:
                # Re-raise circuit-open immediately — do not count as a retry
                raise

            except Exception as exc:
                duration_ms = (time.monotonic() - attempt_start) * 1000
                error_type = self.classify_error(exc)
                last_error = exc

                record = AttemptRecord(
                    attempt=attempt,
                    timestamp=time.time(),
                    success=False,
                    duration_ms=round(duration_ms, 2),
                    error_type=error_type,
                    error_message=str(exc),
                )
                attempts.append(record)

                if circuit_breaker is not None:
                    cb: CircuitBreaker = circuit_breaker
                    await cb.record_result(success=False, error_type=error_type)

                logger.warning(
                    "tool_retry_attempt_failed",
                    tool=tool_name,
                    attempt=attempt,
                    max_retries=self.config.max_retries,
                    error_type=error_type,
                    error_message=str(exc)[:200],
                )

                athena_tool_retry_total.labels(
                    tool_name=tool_name, error_type=error_type,
                ).inc()

                # Non-retryable → stop immediately
                if not self.is_retryable(exc):
                    logger.info(
                        "tool_non_retryable_error",
                        tool=tool_name,
                        error_type=error_type,
                    )
                    break

                # Last attempt → no sleep
                if attempt < self.config.max_retries:
                    delay = self.compute_delay(attempt)
                    logger.info(
                        "tool_retry_waiting",
                        tool=tool_name,
                        next_attempt=attempt + 1,
                        delay_seconds=round(delay, 2),
                    )
                    await asyncio.sleep(delay)

        # All retries exhausted
        return RetryResult(
            success=False,
            error=last_error,
            attempts=attempts,
            total_attempts=len(attempts),
        )
