"""Run 持久化冲突与状态异常。"""


class RunError(ValueError):
    """Run 状态或命令冲突。"""


class RunConflictError(RunError):
    """活跃任务或同幂等键异内容冲突。"""


class RunNotFoundError(LookupError):
    """Run 不存在或不属于当前所有者。"""


class RunStateError(RunError):
    """命令不适用于 Run 当前状态。"""


class RunContextSafetyCommitError(RunError):
    """上下文安全收尾命令未能在有效执行权内安全持久化。"""

    def __init__(self, report_message_id: str | None, reason: str) -> None:
        """保存稳定报告引用和内部收尾原因，供 Worker 避免普通失败覆盖。"""
        super().__init__("run_context_safety_commit_failed")
        self.report_message_id = report_message_id
        self.reason = reason


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
    "RunContextSafetyCommitError",
    "RunError",
    "RunInputResourceError",
    "RunKnowledgeBaseNotFoundError",
    "RunNotFoundError",
    "RunStateError",
    "SessionHasActiveRunError",
    "ToolRetryConfirmationRequiredError",
]
