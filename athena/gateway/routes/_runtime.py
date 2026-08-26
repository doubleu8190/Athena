"""路由层的会话停止事件管理。

会话级停止事件通过 ``get_session_stop_event()`` 管理，
支持 SESSION_STOP 命令和 POST /stop 端点终止正在运行的 Agent。
"""

from __future__ import annotations

import asyncio

# 会话级停止事件（session_id → asyncio.Event）
_stop_events: dict[str, asyncio.Event] = {}


def get_session_stop_event(session_id: str) -> asyncio.Event:
    """获取或创建会话级停止事件。

    每个会话维护一个独立的 ``asyncio.Event``，用于通知正在运行的
    Agent 停止执行。事件由 gateway 层注入到 Harness 及子 Agent。

    参数：
        session_id: 会话 ID。

    返回值：
        该会话的停止事件实例。
    """
    if session_id not in _stop_events:
        _stop_events[session_id] = asyncio.Event()
    return _stop_events[session_id]


def reset_session_stop_event(session_id: str) -> None:
    """重置会话停止事件（新 run 开始时调用，避免粘滞误停）。"""
    if session_id in _stop_events:
        _stop_events[session_id].clear()


def clear_session_stop_event(session_id: str) -> None:
    """清除会话停止事件（run 结束时调用，由 done_callback 触发）。"""
    _stop_events.pop(session_id, None)
