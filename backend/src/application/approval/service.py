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
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `repository`（ApprovalRepository）：负责读取或持久化领域数据的仓储依赖；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._repository = repository

    async def resolve(
        self,
        approval_id: str,
        *,
        decision: str,
    ) -> bool:
        """记录审批决定；归属和 Worker 恢复身份由仓储从审批行读取。

        参数：
        - `approval_id`（str）：目标审批请求的稳定标识；约束：无额外运行时限制。
        - `decision`（str）：经过校验的决策内容，用于驱动后续状态转换；约束：无额外运行时限制。

        返回：
        - bool：操作是否成功或条件是否成立。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        decision = ApprovalDecision(decision)
        return await self._repository.resolve(approval_id, decision=decision)

    async def expire_due_approvals(self, *, limit: int = 64) -> int:
        """将已到期审批写为拒绝并持久化对应 Worker 恢复作业。

        参数：
        - `limit`（int）：一次数据库事务最多处理的审批数量，必须为正数；约束：可省略，默认值为 `64`；须通过方法内校验；失败条件见下方异常说明。

        返回：
        - int：本次成功转换为 EXPIRED 的审批数量。

        异常：
        - ValueError：校验条件 `limit < 1` 成立时（异常说明：limit must be positive）。
        - 未列出的依赖异常按当前实现传播。
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        return await self._repository.expire_due_approvals(limit=limit)

    async def pending(self, session_id: str | None = None) -> list[ApprovalResponse]:
        """查询待审批请求。

        参数：
        - `session_id`（str | None）：目标会话的稳定标识，用于限定数据范围；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - list[ApprovalResponse]：尚待人工处理的审批响应列表，按仓储查询顺序返回。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return [
            _to_response(value)
            for value in await self._repository.list_pending(session_id)
        ]

    async def history(
        self, session_id: str | None = None, *, limit: int = 50, offset: int = 0
    ) -> list[ApprovalResponse]:
        """按筛选条件读取已处理的审批历史。

        参数：
        - `session_id`（str | None）：目标会话的稳定标识，用于限定数据范围；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `limit`（int）：本次读取或处理的数量上限；约束：可省略，默认值为 `50`。
        - `offset`（int）：分页查询时跳过的记录数；约束：可省略，默认值为 `0`。

        返回：
        - list[ApprovalResponse]：符合筛选条件的已处理审批响应列表。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return [
            _to_response(value)
            for value in await self._repository.list_history(
                session_id, limit=limit, offset=offset
            )
        ]

    async def stats(self, session_id: str | None = None) -> ApprovalStatsResponse:
        """返回审批数量统计。

        参数：
        - `session_id`（str | None）：目标会话的稳定标识，用于限定数据范围；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - ApprovalStatsResponse：供调用方展示或继续处理的 `ApprovalStatsResponse` 响应对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        pending = await self.pending(session_id)
        history = await self.history(session_id, limit=100000, offset=0)
        approved = sum(item.resolution == "approved" for item in history)
        denied = sum(item.resolution == "denied" for item in history)
        return ApprovalStatsResponse(
            len(pending), approved, denied, len(pending) + len(history)
        )


def _to_response(value: ApprovalRequest) -> ApprovalResponse:
    """将审批实体转换为只读应用响应。

    参数：
    - `value`（ApprovalRequest）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - ApprovalResponse：供调用方展示或继续处理的 `ApprovalResponse` 响应对象。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
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
