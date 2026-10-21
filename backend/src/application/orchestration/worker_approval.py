"""Worker 中断与持久审批请求之间的应用协调。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from domain.orchestration import WorkerJobLease, WorkerJobRepository


class WorkerApprovalService:
    """持久化 Worker interrupt 审批；通知由事务性 Outbox 投递。"""

    def __init__(self, repository: WorkerJobRepository) -> None:
        """保存 Worker Job 仓储；请求通知与业务状态原子写入 Outbox。"""
        self._repository = repository

    async def request(
        self,
        lease: WorkerJobLease,
        interrupt: Mapping[str, Any],
    ) -> bool:
        """核对中断身份后原子暂停执行并创建审批和可重放通知。

        Args:
            lease: 触发当前 interrupt 的执行租约。
            interrupt: Worker 图持久化的操作描述和 LangGraph interrupt ID。

        Returns:
            仓储接受审批请求并写入通知 Outbox 时返回 True。

        Raises:
            ValueError: 中断缺少必需身份，或与当前执行租约不匹配时抛出。
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
        }
        return await self._repository.wait_for_approval(lease, request)


__all__ = ["WorkerApprovalService"]
