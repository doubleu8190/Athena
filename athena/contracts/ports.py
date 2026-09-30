"""由基础设施适配器实现的 Runtime 端口协议。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from athena.contracts.commands import CommandType
from athena.contracts.events import ApplicationEvent
from athena.contracts.statuses import (
    AgentApprovalDecision,
    AgentCommandStatus,
    AgentRunStatus,
)


@dataclass(frozen=True, slots=True)
class AgentCommandRecord:
    """Runtime 消费命令所需的字段集合。"""

    command_id: str
    session_id: str
    run_id: str | None
    command_type: CommandType
    schema_version: int
    payload_json: str


@dataclass(frozen=True, slots=True)
class AgentRunRecord:
    """启动恢复对账所需的运行字段集合。"""

    run_id: str
    status: AgentRunStatus
    cancel_requested: int


@dataclass(frozen=True, slots=True)
class OrchestrationPlanRecord:
    """启动恢复对账所需的计划轻量记录。"""

    plan_id: str
    session_id: str
    root_run_id: str
    status: str


class AgentStorePort(Protocol):
    async def create_worker_run(
        self,
        *,
        run_id: str,
        session_id: str,
        parent_run_id: str,
        attempt: int,
    ) -> None:
        """创建一条独立 Worker Run 记录。"""
        ...

    async def claim_next_command(self) -> AgentCommandRecord | None:
        """按最早发布时间领取一条可执行命令；无命令时返回 ``None``。"""
        ...


    async def complete_command(
        self,
        command_id: str,
        *,
        status: AgentCommandStatus,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """以终态写入命令结果或错误。"""
        ...

    async def publish(self, event: ApplicationEvent) -> ApplicationEvent:
        """持久化或广播应用事件，并返回最终事件。"""
        ...

    async def list_recoverable_runs(self) -> list[AgentRunRecord]:
        """返回启动恢复所需的运行记录，包括暂停中的运行。"""
        ...

    async def update_run_status(
        self, run_id: str, status: AgentRunStatus, error: str | None = None
    ) -> None:
        """更新运行状态，并可选记录错误信息。"""
        ...

    async def get_run(self, run_id: str) -> Any | None:
        """读取单条运行记录，用于控制命令和执行任务的竞态判定。"""
        ...

    async def update_run_control(
        self,
        run_id: str,
        *,
        pause: bool = False,
        clear_pause: bool = False,
        cancel: bool = False,
        status: AgentRunStatus | None = None,
    ) -> bool:
        """更新运行控制标志及状态；运行不存在时返回 ``False``。"""
        ...


    async def list_approvals_by_batch(
        self, approval_batch_id: str, *, include_resolved: bool = False
    ) -> list[Any]:
        ...

    async def resolve_approval_batch(
        self,
        approval_batch_id: str,
        decisions: dict[str, AgentApprovalDecision],
        *,
        expected_run_id: str | None = None,
    ) -> bool:
        ...

    async def get_message_command_for_run(self, run_id: str) -> AgentCommandRecord | None:
        ...


class EventPublisherPort(Protocol):
    """Runtime 事件接收端口；具体传输和网关实现均属于适配器。"""

    async def publish(self, event: ApplicationEvent) -> ApplicationEvent:
        """将事件发送到运行时事件接收端。"""
        ...

    async def publish_realtime(self, event: ApplicationEvent) -> ApplicationEvent:
        """只向在线订阅者广播实时事件，不写入事件数据库。"""
        ...
