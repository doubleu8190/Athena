"""认证固定窗口限流测试。"""

from __future__ import annotations

from app.auth.rate_limit import FixedWindowRateLimiter


def test_fixed_window_limits_and_resets() -> None:
    now = [100.0]
    limiter = FixedWindowRateLimiter(
        limit=2,
        window_seconds=10,
        clock=lambda: now[0],
    )

    assert limiter.consume("login") is True
    assert limiter.consume("login") is True
    assert limiter.consume("login") is False

    now[0] += 10
    assert limiter.consume("login") is True

    limiter.reset("login")
    assert limiter.consume("login") is True


def test_fixed_window_isolated_by_key() -> None:
    limiter = FixedWindowRateLimiter(
        limit=1,
        window_seconds=10,
        clock=lambda: 1.0,
    )

    assert limiter.consume("first") is True
    assert limiter.consume("first") is False
    assert limiter.consume("second") is True
