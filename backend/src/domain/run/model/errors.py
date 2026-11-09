"""Run 持久化冲突与状态异常。"""


class RunError(ValueError):
    """Run 状态或命令冲突。"""


class RunConflictError(RunError):
    """活跃任务或同幂等键异内容冲突。"""


class RunNotFoundError(LookupError):
    """Run 不存在或不属于当前所有者。"""


class RunStateError(RunError):
    """命令不适用于 Run 当前状态。"""


class SessionHasActiveRunError(RunConflictError):
    """会话存在未结束 Run，不能删除。"""


class ToolRetryConfirmationRequiredError(RunConflictError):
    """来源 Run 含 unknown 调用，必须整体确认后才能重试。"""


__all__ = [
    "RunConflictError",
    "RunError",
    "RunNotFoundError",
    "RunStateError",
    "SessionHasActiveRunError",
    "ToolRetryConfirmationRequiredError",
]
