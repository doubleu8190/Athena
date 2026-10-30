"""Worker 中断与持久审批请求之间的应用协调。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from domain.orchestration import WorkerJobLease, WorkerJobRepository


class WorkerApprovalService:
    """持久化 Worker interrupt 审批；通知由事务性 Outbox 投递。"""

    def __init__(
        self, repository: WorkerJobRepository, *, timeout_seconds: float = 86400.0
    ) -> None:
        """保存 Worker 作业仓储和人工审批等待时限。

        参数：
        - `repository`（WorkerJobRepository）：原子保存审批状态、Outbox 和 Worker Resume Job 的仓储；约束：无额外运行时限制。
        - `timeout_seconds`（float）：用户未决时自动拒绝的时长，必须大于零；约束：可省略，默认值为 `86400.0`；须通过方法内校验；失败条件见下方异常说明。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - ValueError：校验条件 `timeout_seconds <= 0` 成立时（异常说明：approval timeout_seconds must be positive）。
        - 未列出的依赖异常按当前实现传播。
        """
        if timeout_seconds <= 0:
            raise ValueError("approval timeout_seconds must be positive")
        self._repository = repository
        self._timeout_seconds = timeout_seconds

    async def request(
        self,
        lease: WorkerJobLease,
        interrupt: Mapping[str, Any],
    ) -> bool:
        """核对中断身份后原子暂停执行并创建审批和可重放通知。

        参数：
        - `lease`（WorkerJobLease）：触发当前 interrupt 的执行租约；约束：无额外运行时限制。
        - `interrupt`（Mapping[str, Any]）：Worker 图持久化的操作描述和 LangGraph interrupt ID；约束：无额外运行时限制。

        返回：
        - bool：仓储接受审批请求并写入通知 Outbox 时返回 True。

        异常：
        - ValueError：校验条件 `any((not str(approval.get(key, '')).strip() for key in required))` 成立时（异常说明：worker_approval_identity_incomplete）。
        - ValueError：校验条件 `any((approval.get(key) != value for key, value in expected.items()))` 成立时（异常说明：worker_approval_execution_mismatch）。
        - ValueError：校验条件 `not isinstance(arguments, Mapping)` 成立时（异常说明：worker_approval_arguments_invalid）。
        - 未列出的依赖异常按当前实现传播。
        """
        approval = dict(interrupt)
        required = (
            "approval_id",
            "interrupt_id",
            "tool_call_id",
            "tool_name",
            "args_hash",
            "operation_key",
        )
        if any(not str(approval.get(key, "")).strip() for key in required):
            raise ValueError("worker_approval_identity_incomplete")
        expected = {
            "run_id": lease.run_id,
            "plan_id": lease.plan_id,
            "task_id": lease.task_id,
            "execution_id": lease.execution_id,
            "worker_thread_id": lease.worker_thread_id,
        }
        if any(approval.get(key) != value for key, value in expected.items()):
            raise ValueError("worker_approval_execution_mismatch")
        arguments = approval.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise ValueError("worker_approval_arguments_invalid")

        request = {
            **expected,
            "approval_id": str(approval["approval_id"]),
            "interrupt_id": str(approval["interrupt_id"]),
            "session_id": lease.session_id,
            "tool_call_id": str(approval["tool_call_id"]),
            "tool_name": str(approval["tool_name"]),
            "arguments": dict(arguments),
            "risk_level": str(approval.get("risk_level", "high")),
            "args_hash": str(approval["args_hash"]),
            "operation_key": str(approval["operation_key"]),
            "expires_at": datetime.now(timezone.utc)
            + timedelta(seconds=self._timeout_seconds),
        }
        return await self._repository.wait_for_approval(lease, request)


__all__ = ["WorkerApprovalService"]
