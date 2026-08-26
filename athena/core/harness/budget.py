"""预算控制 — maxTurns + retryBudget 双重限制."""

from __future__ import annotations

from dataclasses import dataclass


class BudgetExceeded(Exception):
    """预算超限异常."""


@dataclass
class Budget:
    """执行预算.

    - max_turns: 最大 LLM 调用轮次
    - retry_budget: 最大重试次数
    - turn_count: 当前轮次计数（内存态递增）
    - retry_count: 当前重试计数（工具调用成功后重置）
    """

    max_turns: int = 20
    retry_budget: int = 3
    turn_count: int = 0
    retry_count: int = 0

    def increment_turn(self) -> None:
        """递增轮次计数，超限则抛出 BudgetExceeded."""
        self.turn_count += 1
        if self.turn_count > self.max_turns:
            raise BudgetExceeded(
                f"轮次预算超限: {self.turn_count}/{self.max_turns}"
            )

    def increment_retry(self) -> None:
        """递增重试计数，超限则抛出 BudgetExceeded."""
        self.retry_count += 1
        if self.retry_count > self.retry_budget:
            raise BudgetExceeded(
                f"重试预算超限: {self.retry_count}/{self.retry_budget}"
            )

    def reset_retries(self) -> None:
        """重置重试计数（工具调用成功后调用）."""
        self.retry_count = 0

    def is_exceeded(self) -> bool:
        """执行“是否已超出”操作。

        返回值：
            bool: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return self.turn_count > self.max_turns or self.retry_count > self.retry_budget

    def remaining_turns(self) -> int:
        """执行“remaining turns”操作。

        返回值：
            int: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return max(0, self.max_turns - self.turn_count)

    def remaining_retries(self) -> int:
        """执行“remaining retries”操作。

        返回值：
            int: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return max(0, self.retry_budget - self.retry_count)
