"""路由运行时状态 — 存放跨路由共享的运行时对象（由 main.py 注入）."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from athena.core.agent.workflow import AgentWorkflow

if TYPE_CHECKING:
    from athena.core.files.runtime import FileIntelligenceRuntime
    from athena.core.files.tasks import FileTaskWorker

# 全局 AgentWorkflow 实例（main.py 启动时注入）
_workflow: AgentWorkflow | None = None
_file_runtime: "FileIntelligenceRuntime | None" = None
_file_worker: "FileTaskWorker | None" = None

# 会话级停止事件（session_id → asyncio.Event）
_stop_events: dict[str, asyncio.Event] = {}


def set_workflow(workflow: AgentWorkflow) -> None:
    global _workflow
    _workflow = workflow


def get_workflow() -> AgentWorkflow:
    if _workflow is None:
        raise RuntimeError("Agent workflow is not initialized")
    return _workflow


def set_file_runtime(runtime: "FileIntelligenceRuntime") -> None:
    global _file_runtime
    _file_runtime = runtime


def get_file_runtime() -> "FileIntelligenceRuntime":
    if _file_runtime is None:
        raise RuntimeError("File runtime is not initialized")
    return _file_runtime


def set_file_worker(worker: "FileTaskWorker") -> None:
    global _file_worker
    _file_worker = worker


def get_file_worker() -> "FileTaskWorker":
    if _file_worker is None:
        raise RuntimeError("File worker is not initialized")
    return _file_worker


def get_session_stop_event(session_id: str) -> asyncio.Event:
    """获取或创建会话级停止事件."""
    if session_id not in _stop_events:
        _stop_events[session_id] = asyncio.Event()
    return _stop_events[session_id]


def reset_session_stop_event(session_id: str) -> None:
    """重置会话停止事件（运行结束时调用）."""
    if session_id in _stop_events:
        _stop_events[session_id].clear()


def clear_session_stop_event(session_id: str) -> None:
    _stop_events.pop(session_id, None)
