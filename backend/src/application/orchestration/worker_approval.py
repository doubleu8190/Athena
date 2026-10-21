"""Worker 中断与持久审批请求之间的应用协调。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from domain.events import ApplicationEvent, EventDurability, EventType
from domain.events.ports import EventPort
from domain.orchestration import WorkerJobLease, WorkerJobRepository


class WorkerApprovalService:
    """持久化 Worker interrupt 对应的审批，并发布用户通知。"""

    def __init__(self, repository: WorkerJobRepository, events: EventPort) -> None:
        """保存 Worker Job 仓储和应用事件端口。"""
        self._repository = repository
        self._events = events

    async def request(
        self,
        lease: WorkerJobLease,
        interrupt: Mapping[str, Any],
    ) -> bool:
        """核对中断身份后原子暂停执行、创建审批，并发布可重放事件。

        Args:
            lease: 触发当前 interrupt 的执行租约。
            interrupt: Worker 图持久化的操作描述和 LangGraph interrupt ID。

        Returns:
            仓储接受该审批请求时返回 True。

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
        accepted = await self._repository.wait_for_approval(lease, request)
        if not accepted:
            return False

        await self._events.publish(
            ApplicationEvent(
                event_type=EventType.APPROVAL_REQUIRED,
                durability=EventDurability.DURABLE,
                session_id=lease.session_id,
                run_id=lease.run_id,
                payload={
                    "activity_id": f"approval:{request['approval_id']}",
                    "activity_type": "approval",
                    "parent_activity_id": f"task:{lease.task_id}",
                    "name": "tool_approval",
                    "status": "waiting",
                    **request,
                },
            )
        )
        return True


__all__ = ["WorkerApprovalService"]
