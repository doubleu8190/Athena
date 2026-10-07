"""Application worker facade。"""

from __future__ import annotations

from .scheduler import WorkerSchedulerService


class TaskExecutor(WorkerSchedulerService):
    """阶段 6 目标层 TaskExecutor；具体 Worker 由 port 注入。"""


__all__ = ["TaskExecutor"]
