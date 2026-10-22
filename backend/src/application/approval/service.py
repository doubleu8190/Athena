"""审批应用服务。"""

from __future__ import annotations

from domain.approval import (
    ApprovalDecision,
    ApprovalRepository,
    ApprovalRequest,
    ApprovalStatus,
)
from application.common.responses import ApprovalResponse, ApprovalStatsResponse


class ApprovalService:
    """查询 Worker 审批并记录单条决定。"""

    def __init__(self, repository: ApprovalRepository) -> None:
        self._repository = repository

    async def resolve(
        self,
        approval_id: str,
        *,
        decision: str,
    ) -> bool:
        """记录审批决定；归属和 Worker 恢复身份由仓储从审批行读取。"""
        decision = ApprovalDecision(decision)
        return await self._repository.resolve(approval_id, decision=decision)

    async def expire_due_approvals(self, *, limit: int = 64) -> int:
        """将已到期审批写为拒绝并持久化对应 Worker 恢复作业。

        Args:
            limit: 一次数据库事务最多处理的审批数量，必须为正数。

        Returns:
            本次成功转换为 EXPIRED 的审批数量。

        Raises:
            ValueError: 批量上限小于 1。
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        return await self._repository.expire_due_approvals(limit=limit)

    async def pending(self, session_id: str | None = None) -> list[ApprovalResponse]:
        """查询待审批请求。"""
        return [
            _to_response(value)
            for value in await self._repository.list_pending(session_id)
        ]

    async def history(
        self, session_id: str | None = None, *, limit: int = 50, offset: int = 0
    ) -> list[ApprovalResponse]:
        """查询已处理审批历史。"""
        return [
            _to_response(value)
            for value in await self._repository.list_history(
                session_id, limit=limit, offset=offset
            )
        ]

    async def stats(self, session_id: str | None = None) -> ApprovalStatsResponse:
        """返回审批数量统计。"""
        pending = await self.pending(session_id)
        history = await self.history(session_id, limit=100000, offset=0)
        approved = sum(item.resolution == "approved" for item in history)
        denied = sum(item.resolution == "denied" for item in history)
        return ApprovalStatsResponse(
            len(pending), approved, denied, len(pending) + len(history)
        )


def _to_response(value: ApprovalRequest) -> ApprovalResponse:
    """将审批实体转换为只读应用响应。"""
    return ApprovalResponse(
        approval_id=value.approval_id,
        tool_name=value.tool_name,
        arguments=dict(value.arguments),
        risk_level=value.risk_level,
        created_at=value.created_at,
        session_id=value.session_id,
        run_id=value.run_id,
        tool_call_id=value.tool_call_id,
        approval_batch_id=value.approval_batch_id,
        resolved=value.status != ApprovalStatus.PENDING,
        resolution=value.decision.value if value.decision else value.status.value,
        expires_at=value.expires_at,
        plan_id=value.plan_id,
        task_id=value.task_id,
    )


__all__ = ["ApprovalService"]
