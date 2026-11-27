"""跨层共享的纯 Python 退避重试计算辅助函数（无第三方框架依赖）。"""

from __future__ import annotations

import random


def compute_worker_retry_delay(
    consecutive_failures: int,
    *,
    base_seconds: float = 0.5,
    max_seconds: float = 30.0,
) -> float:
    """计算有界全抖动指数退避延迟（秒），防止瞬态故障恢复期惊群重试。"""
    # 将失败次数裁剪到 [0, 6] 区间，避免指数上溢并维持合理上限。
    exponent = min(max(consecutive_failures - 1, 0), 6)
    ceiling = min(max_seconds, base_seconds * (2**exponent))
    return random.uniform(0.0, ceiling)


__all__ = ["compute_worker_retry_delay"]
