"""工具错误自愈路由器 + 熔断器.

集成到 Harness._execute_single_tool()，工具失败时先查 Fallback 路由表
进行确定性恢复，无可行 Fallback 时才将错误返回给 LLM。

熔断器三态（project_memory 约束）：CLOSED / OPEN / HALF_OPEN，HALF_OPEN 探测失败回退 OPEN。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from enum import StrEnum
from typing import Any, Callable

from athena.utils.logging import get_logger

logger = get_logger(__name__)


class CircuitState(StrEnum):
    """熔断器状态."""

    CLOSED = "closed"  # 正常调用
    OPEN = "open"  # 熔断，直接走 fallback
    HALF_OPEN = "half_open"  # 半开，允许探测


class CircuitBreaker:
    """熔断器 - 滑动窗口失败率跟踪."""

    def __init__(
        self,
        failure_threshold: float = 0.8,
        window_size: int = 10,
        open_duration_s: float = 30.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._window_size = window_size
        self._open_duration_s = open_duration_s
        # tool_name → deque[(success: bool, timestamp: float)]
        self._records: dict[str, deque[tuple[bool, float]]] = defaultdict(deque)
        self._states: dict[str, CircuitState] = defaultdict(lambda: CircuitState.CLOSED)
        self._opened_at: dict[str, float] = {}

    def record_result(self, tool_name: str, success: bool) -> None:
        """记录一次调用结果."""
        now = time.time()
        records = self._records[tool_name]
        records.append((success, now))
        if len(records) > self._window_size:
            records.popleft()

        state = self._get_state(tool_name)
        if state == CircuitState.HALF_OPEN:
            if not success:
                # 探测失败 → 回退 OPEN（project_memory 约束）
                self._states[tool_name] = CircuitState.OPEN
                self._opened_at[tool_name] = now
                logger.warning("circuit_breaker_reopened", tool=tool_name)
            else:
                # 探测成功 → 关闭
                self._states[tool_name] = CircuitState.CLOSED
                self._opened_at.pop(tool_name, None)
                logger.info("circuit_breaker_closed", tool=tool_name)
        elif state == CircuitState.CLOSED:
            failure_rate = self._failure_rate(tool_name)
            if failure_rate >= self._failure_threshold and len(records) >= 3:
                self._states[tool_name] = CircuitState.OPEN
                self._opened_at[tool_name] = now
                logger.warning(
                    "circuit_breaker_opened",
                    tool=tool_name,
                    failure_rate=round(failure_rate, 2),
                )

    def is_open(self, tool_name: str) -> bool:
        """检查熔断器是否打开（调用方应走 fallback）."""
        state = self._get_state(tool_name)
        return state in (CircuitState.OPEN, CircuitState.HALF_OPEN)

    def _get_state(self, tool_name: str) -> CircuitState:
        """获取当前状态，OPEN 超时后转为 HALF_OPEN."""
        state = self._states[tool_name]
        if state == CircuitState.OPEN:
            opened_at = self._opened_at.get(tool_name, 0)
            if time.time() - opened_at >= self._open_duration_s:
                self._states[tool_name] = CircuitState.HALF_OPEN
                logger.info("circuit_breaker_half_open", tool=tool_name)
                return CircuitState.HALF_OPEN
        return state

    def _failure_rate(self, tool_name: str) -> float:
        records = self._records[tool_name]
        if not records:
            return 0.0
        failures = sum(1 for success, _ in records if not success)
        return failures / len(records)

    def reset(self, tool_name: str | None = None) -> None:
        """重置熔断器状态（测试用）."""
        if tool_name is None:
            self._records.clear()
            self._states.clear()
            self._opened_at.clear()
        else:
            self._records.pop(tool_name, None)
            self._states.pop(tool_name, None)
            self._opened_at.pop(tool_name, None)


class FallbackRoute:
    """单条 Fallback 路由配置."""

    def __init__(
        self,
        tool_name: str,
        on_errors: list[str],
        action: str,  # retry / alternate / escalate / passthrough
        max_retries: int = 1,
        param_transform: dict[str, Callable[[Any], Any]] | None = None,
        alternate_tool: str | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.on_errors = on_errors
        self.action = action
        self.max_retries = max_retries
        self.param_transform = param_transform or {}
        self.alternate_tool = alternate_tool


class ToolErrorHandler:
    """工具错误自愈路由器."""

    def __init__(self, circuit_breaker: CircuitBreaker | None = None) -> None:
        self._circuit = circuit_breaker or CircuitBreaker()
        self._routes: dict[tuple[str, str], FallbackRoute] = {}

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit

    def register_fallback(
        self,
        tool_name: str,
        on_errors: list[str],
        action: str,
        max_retries: int = 1,
        param_transform: dict[str, Callable[[Any], Any]] | None = None,
        alternate_tool: str | None = None,
    ) -> None:
        """注册 Fallback 路由."""
        for err in on_errors:
            self._routes[(tool_name, err)] = FallbackRoute(
                tool_name=tool_name,
                on_errors=on_errors,
                action=action,
                max_retries=max_retries,
                param_transform=param_transform,
                alternate_tool=alternate_tool,
            )
        logger.info(
            "fallback_route_registered",
            tool=tool_name,
            action=action,
            on_errors=on_errors,
        )

    def get_fallback(self, tool_name: str, error: Exception) -> FallbackRoute | None:
        """查找 Fallback 路由."""
        err_msg = str(error).lower()
        # 精确匹配错误类别
        for (name, err_key), route in self._routes.items():
            if name != tool_name:
                continue
            if err_key in err_msg or err_key in type(error).__name__.lower():
                return route
        return None

    def is_circuit_open(self, tool_name: str) -> bool:
        return self._circuit.is_open(tool_name)

    def record_result(self, tool_name: str, success: bool) -> None:
        self._circuit.record_result(tool_name, success)

    def apply_param_transform(
        self, params: dict[str, Any], route: FallbackRoute
    ) -> dict[str, Any]:
        """应用参数变换."""
        new_params = dict(params)
        for key, transform in route.param_transform.items():
            if key in new_params:
                try:
                    new_params[key] = transform(new_params[key])
                except Exception as e:
                    logger.warning("param_transform_failed", key=key, error=str(e))
        return new_params
