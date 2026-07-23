"""Per-tool circuit breaker with failure-rate-based tripping.

States:
    CLOSED   — normal operation, requests pass through.
    OPEN     — tripped, requests are fast-failed.
    HALF_OPEN — recovery probe, a limited number of requests are allowed.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum

from athena.logging_config import get_logger
from athena.api.metrics import athena_tool_circuit_state, athena_tool_circuit_trip_total

logger = get_logger(__name__)

# State value mapping for Prometheus Gauge
_STATE_VALUES: dict[str, float] = {"closed": 0.0, "open": 1.0, "half_open": 2.0}


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    """Configuration for a single circuit breaker."""

    failure_rate_threshold: float = 0.5
    min_requests: int = 20
    open_duration_seconds: int = 30
    half_open_max_calls: int = 3
    half_open_success_threshold: int = 2
    sliding_window_size: int = 100


@dataclass(frozen=True)
class _CallRecord:
    timestamp: float
    success: bool
    error_type: str | None = None


class CircuitBreaker:
    """Failure-rate-based circuit breaker keyed by tool name."""

    def __init__(self, tool_name: str, config: CircuitBreakerConfig) -> None:
        self.tool_name = tool_name
        self.config = config
        self._state = CircuitState.CLOSED
        self._records: deque[_CallRecord] = deque(
            maxlen=config.sliding_window_size
        )
        self._open_since: float = 0.0
        self._half_open_calls: int = 0
        self._half_open_successes: int = 0
        self._lock = asyncio.Lock()

    # ── State with lazy open→half_open transition ──────────────────

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._open_since
            if elapsed >= self.config.open_duration_seconds:
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 0
                self._half_open_successes = 0
                logger.info("circuit_half_open", tool=self.tool_name)
        return self._state

    # ── Public API ─────────────────────────────────────────────────

    async def allow_request(self) -> bool:
        """Return ``True`` if the caller is allowed to proceed."""
        async with self._lock:
            current = self.state
            if current == CircuitState.CLOSED:
                return True
            if current == CircuitState.HALF_OPEN:
                if self._half_open_calls < self.config.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                return False
            # OPEN
            return False

    async def record_result(
        self, success: bool, error_type: str | None = None
    ) -> None:
        """Record one call outcome and update breaker state."""
        async with self._lock:
            self._records.append(
                _CallRecord(
                    timestamp=time.monotonic(),
                    success=success,
                    error_type=error_type,
                )
            )

            if self._state == CircuitState.HALF_OPEN:
                self._handle_half_open_result(success)
            elif self._state == CircuitState.CLOSED:
                self._maybe_trip()

    async def reset(self) -> None:
        """Manually reset the breaker to CLOSED (e.g. after admin intervention)."""
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._records.clear()
            self._half_open_calls = 0
            self._half_open_successes = 0
            logger.info("circuit_reset", tool=self.tool_name)

    def get_metrics(self) -> dict:
        """Return current breaker metrics (thread-safe snapshot)."""
        records = list(self._records)
        total = len(records)
        failures = sum(1 for r in records if not r.success)
        return {
            "tool_name": self.tool_name,
            "state": self.state.value,
            "total_requests": total,
            "failures": failures,
            "failure_rate": failures / total if total > 0 else 0.0,
        }

    # ── Internal helpers ───────────────────────────────────────────

    def _handle_half_open_result(self, success: bool) -> None:
        if success:
            self._half_open_successes += 1
            if (
                self._half_open_successes
                >= self.config.half_open_success_threshold
            ):
                self._state = CircuitState.CLOSED
                self._records.clear()
                athena_tool_circuit_state.labels(tool_name=self.tool_name).set(_STATE_VALUES["closed"])
                logger.info("circuit_closed", tool=self.tool_name)
        else:
            self._state = CircuitState.OPEN
            self._open_since = time.monotonic()
            athena_tool_circuit_state.labels(tool_name=self.tool_name).set(_STATE_VALUES["open"])
            athena_tool_circuit_trip_total.labels(tool_name=self.tool_name).inc()
            logger.warning("circuit_reopened", tool=self.tool_name)

    def _maybe_trip(self) -> None:
        records = list(self._records)
        if len(records) < self.config.min_requests:
            return
        failures = sum(1 for r in records if not r.success)
        failure_rate = failures / len(records)
        # Check failure rate threshold, trip if exceeded
        if failure_rate >= self.config.failure_rate_threshold:
            self._state = CircuitState.OPEN
            self._open_since = time.monotonic()
            athena_tool_circuit_state.labels(tool_name=self.tool_name).set(_STATE_VALUES["open"])
            athena_tool_circuit_trip_total.labels(tool_name=self.tool_name).inc()
            logger.warning(
                "circuit_opened",
                tool=self.tool_name,
                failure_rate=round(failure_rate, 3),
                window_size=len(records),
            )


class CircuitBreakerManager:
    """Manages per-tool circuit breaker instances."""

    def __init__(self, config: CircuitBreakerConfig) -> None:
        self._config = config
        self._breakers: dict[str, CircuitBreaker] = {}

    def get_breaker(self, tool_name: str) -> CircuitBreaker:
        if tool_name not in self._breakers:
            self._breakers[tool_name] = CircuitBreaker(
                tool_name, self._config
            )
        return self._breakers[tool_name]

    def get_all_metrics(self) -> list[dict]:
        return [b.get_metrics() for b in self._breakers.values()]
