"""Run 持久化冲突与状态异常。"""


class RunError(ValueError):
    """Run 状态或命令冲突。"""


class RunConflictError(RunError):
    """活跃任务或同幂等键异内容冲突。"""


class RunNotFoundError(LookupError):
    """Run 不存在或不属于当前所有者。"""


class RunStateError(RunError):
    """命令不适用于 Run 当前状态。"""


class RunInputResourceError(RunError):
    """提交或澄清附带的技能/文件/模型资源已不可用或越权。"""


class RunKnowledgeBaseNotFoundError(LookupError):
    """显式指定的知识库不存在、已删除或当前用户无读权限（按 404 防跨部门探测）。"""


class SessionHasActiveRunError(RunConflictError):
    """会话存在未结束 Run，不能删除或变更项目归属。"""


class ToolRetryConfirmationRequiredError(RunConflictError):
    """来源 Run 含 unknown 调用，必须整体确认后才能重试。"""


__all__ = [
    "RunConflictError",
    "RunError",
    "RunInputResourceError",
    "RunKnowledgeBaseNotFoundError",
    "RunNotFoundError",
    "RunStateError",
    "SessionHasActiveRunError",
    "ToolRetryConfirmationRequiredError",
]
