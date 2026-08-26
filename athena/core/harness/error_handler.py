"""工具错误自愈路由器 + 熔断器.

核心职责:
- Fallback 路由表: 工具失败时按 (tool_name, error_keyword) 匹配恢复策略
- 熔断器: 滑动窗口跟踪失败率，三态切换（CLOSED → OPEN → HALF_OPEN）

集成方式:
    集成到 ``Harness._execute_single_tool()``，工具失败时先查 Fallback 路由表
    进行确定性恢复（retry / alternate / passthrough），无可行 Fallback 时
    才将错误返回给 LLM。

熔断器三态约束（project_memory）:
- CLOSED: 正常调用，失败率达阈值且样本 ≥ 3 时转 OPEN
- OPEN: 熔断，直接走 Fallback；超时后转 HALF_OPEN
- HALF_OPEN: 允许单次探测，失败回退 OPEN，成功转 CLOSED
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from enum import StrEnum
from typing import Any, Callable

from athena.utils.logging import get_logger

logger = get_logger(__name__)


class CircuitState(StrEnum):
    """熔断器三态.

    状态转换路径:
        CLOSED --(失败率达阈值)--> OPEN --(超时)--> HALF_OPEN
        HALF_OPEN --(探测成功)--> CLOSED
        HALF_OPEN --(探测失败)--> OPEN
    """

    CLOSED = "closed"
    """正常调用状态，滑动窗口跟踪失败率。"""

    OPEN = "open"
    """熔断状态，拒绝调用并直接走 Fallback 路由。"""

    HALF_OPEN = "half_open"
    """半开状态，允许单次探测调用以判断是否恢复。"""


class CircuitBreaker:
    """熔断器 — 按工具维度独立跟踪的滑动窗口失败率.

    每个工具独立维护调用记录、状态和开启时间。
    状态转换由 ``record_result()`` 驱动，``_get_state()`` 负责
    OPEN → HALF_OPEN 的超时自动转换。

    参数：
        failure_threshold: 失败率阈值 (0~1)，达到此值且样本充足时触发熔断。
        window_size: 滑动窗口大小（最近 N 次调用）。
        open_duration_s: OPEN 状态持续时间（秒），超时后自动转 HALF_OPEN。
    """

    def __init__(
        self,
        failure_threshold: float = 0.8,
        window_size: int = 10,
        open_duration_s: float = 30.0,
    ) -> None:
        """初始化熔断器.

        参数：
            failure_threshold: 失败率阈值，默认 0.8（80% 失败触发熔断）。
            window_size: 滑动窗口大小，默认最近 10 次调用。
            open_duration_s: 熔断持续时间，默认 30 秒后转半开。
        """
        self._failure_threshold = failure_threshold
        self._window_size = window_size
        self._open_duration_s = open_duration_s
        # 按工具维度独立维护: tool_name → deque[(success, timestamp)]
        self._records: dict[str, deque[tuple[bool, float]]] = defaultdict(deque)
        self._states: dict[str, CircuitState] = defaultdict(lambda: CircuitState.CLOSED)
        self._opened_at: dict[str, float] = {}

    def record_result(self, tool_name: str, success: bool) -> None:
        """记录一次工具调用结果并驱动状态转换.

        根据当前状态执行不同的转换逻辑:
        - HALF_OPEN: 探测成功 → CLOSED；探测失败 → OPEN
        - CLOSED: 失败率达阈值且样本 ≥ 3 → OPEN
        - OPEN: 不做转换（等待超时自动转 HALF_OPEN）

        参数：
            tool_name: 工具名称。
            success: 调用是否成功。
        """
        now = time.time()
        records = self._records[tool_name]
        records.append((success, now))
        # 滑动窗口: 超出窗口大小时移除最旧记录
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
                # 探测成功 → 关闭熔断器
                self._states[tool_name] = CircuitState.CLOSED
                self._opened_at.pop(tool_name, None)
                logger.info("circuit_breaker_closed", tool=tool_name)
        elif state == CircuitState.CLOSED:
            failure_rate = self._failure_rate(tool_name)
            # 样本不足（< 3）时不触发熔断，避免少量失败误判
            if failure_rate >= self._failure_threshold and len(records) >= 3:
                self._states[tool_name] = CircuitState.OPEN
                self._opened_at[tool_name] = now
                logger.warning(
                    "circuit_breaker_opened",
                    tool=tool_name,
                    failure_rate=round(failure_rate, 2),
                )

    def is_open(self, tool_name: str) -> bool:
        """检查熔断器是否处于 OPEN 状态.

        参数：
            tool_name: 工具名称。

        返回值：
            True 表示熔断器打开，调用方应走 Fallback 路由。
        """
        state = self._get_state(tool_name)
        return state == CircuitState.OPEN

    def _get_state(self, tool_name: str) -> CircuitState:
        """获取工具的当前熔断状态.

        具有副作用: OPEN 状态超过 ``open_duration_s`` 后自动转为 HALF_OPEN。

        参数：
            tool_name: 工具名称。

        返回值：
            当前熔断状态。
        """
        state = self._states[tool_name]
        if state == CircuitState.OPEN:
            opened_at = self._opened_at.get(tool_name, 0)
            # OPEN 超时 → 自动转 HALF_OPEN，允许探测
            if time.time() - opened_at >= self._open_duration_s:
                self._states[tool_name] = CircuitState.HALF_OPEN
                logger.info("circuit_breaker_half_open", tool=tool_name)
                return CircuitState.HALF_OPEN
        return state

    def _failure_rate(self, tool_name: str) -> float:
        """计算滑动窗口内的失败率.

        参数：
            tool_name: 工具名称。

        返回值：
            失败率 (0~1)，无记录时返回 0.0。
        """
        records = self._records[tool_name]
        if not records:
            return 0.0
        failures = sum(1 for success, _ in records if not success)
        return failures / len(records)

    def reset(self, tool_name: str | None = None) -> None:
        """重置熔断器状态.

        参数：
            tool_name: 指定工具名称时仅重置该工具；为 None 时重置所有工具。
        """
        if tool_name is None:
            self._records.clear()
            self._states.clear()
            self._opened_at.clear()
        else:
            self._records.pop(tool_name, None)
            self._states.pop(tool_name, None)
            self._opened_at.pop(tool_name, None)


class FallbackRoute:
    """单条 Fallback 路由配置.

    定义工具失败时的恢复策略，由 ``ToolErrorHandler.register_fallback()`` 注册。

    属性：
        tool_name: 目标工具名称。
        on_errors: 触发此路由的错误关键词列表（匹配异常消息或类型名）。
        action: 恢复动作，可选值:
            - ``"retry"``: 重试，支持参数变换（param_transform）
            - ``"alternate"``: 切换到备用工具（alternate_tool）
            - ``"passthrough"``: 返回空结果，视为成功
            - ``"escalate"``: 升级错误，直接返回失败
        max_retries: 最大重试次数（仅 action="retry" 时生效）。
        param_transform: 参数变换函数字典，键为参数名，值为变换函数。
        alternate_tool: 备用工具名称（仅 action="alternate" 时生效）。
    """

    def __init__(
        self,
        tool_name: str,
        on_errors: list[str],
        action: str,
        max_retries: int = 1,
        param_transform: dict[str, Callable[[Any], Any]] | None = None,
        alternate_tool: str | None = None,
    ) -> None:
        """初始化 Fallback 路由.

        参数：
            tool_name: 目标工具名称。
            on_errors: 错误关键词列表。
            action: 恢复动作 ("retry" / "alternate" / "escalate" / "passthrough")。
            max_retries: 最大重试次数，默认 1。
            param_transform: 参数变换函数字典。
            alternate_tool: 备用工具名称。
        """
        self.tool_name = tool_name
        self.on_errors = on_errors
        self.action = action
        self.max_retries = max_retries
        self.param_transform = param_transform or {}
        self.alternate_tool = alternate_tool


class ToolErrorHandler:
    """工具错误自愈路由器.

    组合熔断器（CircuitBreaker）与 Fallback 路由表，为 Harness 提供
    工具失败时的确定性恢复能力。

    典型用法::

        handler = ToolErrorHandler()
        handler.register_fallback("exec_shell", ["timeout"], "retry", max_retries=1)
        route = handler.get_fallback("exec_shell", TimeoutError("timed out"))
    """

    def __init__(self, circuit_breaker: CircuitBreaker | None = None) -> None:
        """初始化错误处理器.

        参数：
            circuit_breaker: 自定义熔断器实例，未提供时使用默认配置。
        """
        self._circuit = circuit_breaker or CircuitBreaker()
        self._routes: dict[tuple[str, str], FallbackRoute] = {}

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """获取内部熔断器实例.

        返回值：
            CircuitBreaker 实例。
        """
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
        """注册 Fallback 路由.

        同一工具可注册多条路由，按 on_errors 中的关键词匹配。
        匹配逻辑: 异常消息或异常类名中包含 err_key 即命中。

        参数：
            tool_name: 目标工具名称。
            on_errors: 错误关键词列表，每个关键词对应一条路由。
            action: 恢复动作 ("retry" / "alternate" / "escalate" / "passthrough")。
            max_retries: 最大重试次数（仅 action="retry" 时生效）。
            param_transform: 参数变换函数字典（仅 action="retry" 时生效）。
            alternate_tool: 备用工具名称（仅 action="alternate" 时生效）。
        """
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
        """根据工具名称和异常查找匹配的 Fallback 路由.

        匹配规则: 遍历该工具的所有路由，检查 err_key 是否出现在
        异常消息（str(error)）或异常类名（type(error).__name__）中。

        参数：
            tool_name: 工具名称。
            error: 工具调用抛出的异常。

        返回值：
            匹配的 FallbackRoute，无匹配时返回 None。
        """
        err_msg = str(error).lower()
        # 按 (tool_name, err_key) 精确匹配错误类别
        for (name, err_key), route in self._routes.items():
            if name != tool_name:
                continue
            if err_key in err_msg or err_key in type(error).__name__.lower():
                return route
        return None

    def is_circuit_open(self, tool_name: str) -> bool:
        """检查工具的熔断器是否打开.

        参数：
            tool_name: 工具名称。

        返回值：
            True 表示熔断，调用方应走 Fallback 路由而非直接调用工具。
        """
        return self._circuit.is_open(tool_name)

    def record_result(self, tool_name: str, success: bool) -> None:
        """记录工具调用结果到熔断器.

        参数：
            tool_name: 工具名称。
            success: 调用是否成功。
        """
        self._circuit.record_result(tool_name, success)

    def apply_param_transform(
        self, params: dict[str, Any], route: FallbackRoute
    ) -> dict[str, Any]:
        """按路由配置对工具参数进行变换.

        常见场景: 工具超时后将 timeout 参数翻倍再重试。
        变换函数抛出异常时跳过该参数（保留原值），不中断整体流程。

        参数：
            params: 原始工具调用参数。
            route: 包含 param_transform 配置的 FallbackRoute。

        返回值：
            变换后的新参数字典（不修改原字典）。
        """
        new_params = dict(params)
        for key, transform in route.param_transform.items():
            if key in new_params:
                try:
                    new_params[key] = transform(new_params[key])
                except Exception as e:
                    # 变换失败不中断，保留原值
                    logger.warning("param_transform_failed", key=key, error=str(e))
        return new_params
