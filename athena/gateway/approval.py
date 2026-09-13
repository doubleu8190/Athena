"""持久化审批服务。SQLite 记录是审批状态的唯一事实来源。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from athena.runtime.transport import RuntimeEventPublisher
from athena.contracts.statuses import AgentApprovalDecision, AgentApprovalStatus
from athena.contracts.events import ApplicationEvent, EventDurability, EventType
from athena.infrastructure.sqlite.agent_store import AgentStore
from athena.infrastructure.sqlite.database import Database
from athena.models.approval import ApprovalDecision, ApprovalRequest
from athena.models.tool import RiskLevel
from athena.utils.ids import generate_time_id


class ApprovalManager:
    """协调审批请求的持久化、等待和事件通知。"""

    def __init__(
        self,
        event_publisher: RuntimeEventPublisher,
        db: Database,
        agent_store: AgentStore,
        approval_timeout: int = 120,
    ) -> None:
        """创建审批管理器。

        参数:
            event_publisher (RuntimeEventPublisher): 应用事件发布器。
            db (Database): 兼容层数据库依赖。
            agent_store (AgentStore): 审批状态的持久化存储。
            approval_timeout (int): 默认审批超时时间，单位为秒，必须为正数。
        返回值:
            None。
        异常:
            不抛出业务异常。
        """
        self._event_publisher = event_publisher
        self._db = db
        self._timeout = approval_timeout
        self._agent_store = agent_store
        self._requests: dict[str, ApprovalRequest] = {}

    async def request_approval(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        risk_level: RiskLevel,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        plan_id: str | None = None,
        task_id: str | None = None,
        worker_run_id: str | None = None,
    ) -> ApprovalRequest:
        """创建审批请求、持久化记录并发布待审批事件。

        参数:
            tool_name (str): 待执行工具名称。
            arguments (dict[str, Any]): 工具调用参数。
            risk_level (RiskLevel): 工具风险等级。
            session_id (str): 所属会话 ID。
            run_id (str): 所属运行 ID。
            tool_call_id (str): 工具调用 ID。
        返回值:
            ApprovalRequest: 已创建的审批请求对象。
        异常:
            RuntimeError: 未配置审批持久化存储。
            数据库或事件发布异常: 持久化或通知失败时传播。
        """
        if self._agent_store is None:
            raise RuntimeError("approval persistence is not configured")
        approval_id = generate_time_id()
        request = ApprovalRequest(
            id=approval_id,
            tool_name=tool_name,
            arguments=arguments,
            risk_level=risk_level.value,
            timeout=self._timeout,
            created_at=datetime.now(timezone.utc),
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
        )
        self._requests[approval_id] = request
        await self._agent_store.create_approval(
            approval_id=approval_id,
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            risk_level=risk_level,
            plan_id=plan_id,
            task_id=task_id,
            worker_run_id=worker_run_id,
        )
        await self._event_publisher.publish(
            ApplicationEvent(
                event_type=EventType.APPROVAL_REQUIRED,
                durability=EventDurability.DURABLE,
                session_id=session_id,
                run_id=run_id,
                payload={
                    "approval_id": approval_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "risk_level": risk_level.value,
                    "timeout": self._timeout,
                    "tool_call_id": tool_call_id,
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "worker_run_id": worker_run_id,
                },
            )
        )
        return request

    async def wait_for_decision(self, approval_id: str, timeout: float) -> bool:
        """轮询审批记录直到得到决定或超时。

        参数:
            approval_id (str): 要等待的审批 ID。
            timeout (float): 最长等待时间，单位为秒，必须为非负数。
        返回值:
            bool: 决定为批准时返回 ``True``，拒绝、取消或超时时返回 ``False``。
        异常:
            存储查询失败时传播底层异常；任务取消时传播 ``asyncio.CancelledError``。
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            record = await self._agent_store.get_approval(approval_id)
            if (
                record is not None
                and AgentApprovalStatus(record.status) != AgentApprovalStatus.PENDING
            ):
                decision = AgentApprovalDecision(
                    record.decision or AgentApprovalDecision.DENIED.value
                )
                request = self._requests.get(approval_id)
                if request is not None:
                    request.resolved, request.resolution = True, decision.value
                    request.decided_at = datetime.now(timezone.utc)
                return decision == AgentApprovalDecision.APPROVED
            await asyncio.sleep(0.1)
        request = self._requests.get(approval_id)
        if request is not None:
            request.resolved, request.resolution = True, ApprovalDecision.TIMEOUT.value
            request.decided_at = datetime.now(timezone.utc)
        await self._agent_store.resolve_approval(
            approval_id, AgentApprovalDecision.EXPIRED
        )
        return False

    async def respond_approval(self, approval_id: str, action: str) -> bool:
        """将外部审批动作转换为持久化决定。

        参数:
            approval_id (str): 审批 ID。
            action (str): 动作文本；``allow``、``approve`` 及 ``approved`` 表示批准，其余值表示拒绝。
        返回值:
            bool: 成功解析待审批记录时返回 ``True``，否则返回 ``False``。
        异常:
            存储写入失败时传播底层异常。
        """
        decision = (
            AgentApprovalDecision.APPROVED
            if action.lower() in {"allow", "approve", "approved"}
            else AgentApprovalDecision.DENIED
        )
        return await self._agent_store.resolve_approval(approval_id, decision)

    async def cancel_approval(self, approval_id: str) -> None:
        """取消一个审批请求，并按拒绝处理。

        参数:
            approval_id (str): 审批 ID。
        返回值:
            None: 请求已提交；目标不存在或已处理时保持幂等。
        异常:
            存储写入失败时传播底层异常。
        """
        await self.respond_approval(approval_id, "deny")

    async def cancel_all_pending(self, session_id: str) -> None:
        """取消指定会话的全部未解决审批请求。

        参数:
            session_id (str): 会话 ID；空字符串表示处理全部会话。
        返回值:
            None: 所有匹配请求均已尝试取消。
        异常:
            任一存储写入失败时传播底层异常。
        """
        for approval_id, request in list(self._requests.items()):
            if not session_id or request.session_id == session_id:
                await self.cancel_approval(approval_id)

    def get_pending(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """返回内存索引中的未解决审批请求。

        参数:
            session_id (str | None): 可选会话过滤条件。
        返回值:
            list[dict[str, Any]]: 面向 API 的审批请求字典列表。
        异常:
            不抛出业务异常。
        """
        requests = [
            r
            for r in self._requests.values()
            if not r.resolved and (not session_id or r.session_id == session_id)
        ]
        return [
            {
                "approval_id": r.id,
                "tool_name": r.tool_name,
                "arguments": r.arguments,
                "risk_level": r.risk_level,
                "created_at": r.created_at.isoformat(),
                "session_id": r.session_id,
                "run_id": r.run_id,
                "tool_call_id": r.tool_call_id,
                "resolved": r.resolved,
                "resolution": r.resolution,
            }
            for r in requests
        ]
