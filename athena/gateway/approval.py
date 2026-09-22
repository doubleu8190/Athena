"""持久化审批服务。SQLite 记录是审批状态的唯一事实来源。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from athena.contracts.events import ApplicationEvent, EventDurability, EventType
from athena.contracts.statuses import AgentApprovalDecision, AgentApprovalStatus
from athena.infrastructure.postgre.repositories.agent_store import AgentStore
from athena.infrastructure.postgre.database import Database
from athena.models.approval import ApprovalDecision, ApprovalRequest
from athena.models.tool import RiskLevel
from athena.runtime.transport import RuntimeEventPublisher
from athena.utils.id_generation import generate_time_id


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
            db (Database): 兼容旧调用方的数据库依赖。
            agent_store (AgentStore): 审批状态的持久化存储。
            approval_timeout (int): 新审批的默认超时时间，单位为秒。
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
        self._waiters: dict[str, asyncio.Future[AgentApprovalDecision]] = {}
        self._waiters_lock = asyncio.Lock()

    @property
    def timeout(self) -> int:
        """返回新审批请求使用的默认超时时间。"""
        return self._timeout

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
        approval_id: str | None = None,
    ) -> ApprovalRequest:
        """创建或恢复一次工具尝试对应的审批。

        参数:
            tool_name (str): 待执行工具名称。
            arguments (dict[str, Any]): 工具调用参数快照。
            risk_level (RiskLevel): 工具风险等级。
            session_id (str): 所属会话 ID。
            run_id (str): 所属运行 ID。
            tool_call_id (str): 工具尝试账本 ID；同一 ID 始终复用同一审批。
            plan_id (str | None): 可选执行计划 ID。
            task_id (str | None): 可选稳定任务 ID。
            worker_run_id (str | None): 可选 Worker Run ID。
            approval_id (str | None): 已知审批 ID，重放时用于直接读取原记录。
        返回值:
            ApprovalRequest: 新建或从数据库恢复的审批请求。
        异常:
            RuntimeError: 未配置审批持久化存储。
            数据库或事件发布异常: 持久化或通知失败时传播。
        """
        if self._agent_store is None:
            raise RuntimeError("approval persistence is not configured")

        # 工具节点可能因为 checkpoint 重放再次进入。优先按 attempt 的账本 ID
        # 查找已有审批，避免同一次尝试生成第二个审批窗口。
        record = None
        if approval_id:
            record = await self._agent_store.get_approval(approval_id)
        if record is None:
            finder = getattr(self._agent_store, "get_approval_for_tool_call", None)
            if finder is not None:
                record = await finder(tool_call_id)
        if record is not None:
            request = self._request_from_record(record)
            if request.expires_at is None:
                request.timeout = self._timeout
            self._requests[request.id] = request
            return request

        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(seconds=self._timeout)
        generated_id = approval_id or generate_time_id()
        request = ApprovalRequest(
            id=generated_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            risk_level=risk_level.value,
            timeout=self._timeout,
            created_at=created_at,
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            expires_at=expires_at,
        )
        created = await self._agent_store.create_approval(
            approval_id=generated_id,
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            risk_level=risk_level,
            plan_id=plan_id,
            task_id=task_id,
            worker_run_id=worker_run_id,
            expires_at=expires_at.isoformat(),
        )
        if created is False:
            # 另一个恢复执行者已经建立了记录；只复用它，不再重复发事件。
            existing = await self._agent_store.get_approval(generated_id)
            if existing is None:
                finder = getattr(self._agent_store, "get_approval_for_tool_call", None)
                existing = await finder(tool_call_id) if finder is not None else None
            if existing is None:
                raise RuntimeError("approval was not created and cannot be recovered")
            request = self._request_from_record(existing)
            if request.expires_at is None:
                request.timeout = self._timeout
            self._requests[request.id] = request
            return request

        self._requests[generated_id] = request
        await self._event_publisher.publish(
            ApplicationEvent(
                event_type=EventType.APPROVAL_REQUIRED,
                durability=EventDurability.DURABLE,
                session_id=session_id,
                run_id=run_id,
                payload={
                    "approval_id": generated_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "risk_level": risk_level.value,
                    "timeout": self._timeout,
                    "expires_at": expires_at.isoformat(),
                    "tool_call_id": tool_call_id,
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "worker_run_id": worker_run_id,
                },
            )
        )
        return request

    async def wait_for_decision(self, approval_id: str, timeout: float) -> bool:
        """通过 Future 等待审批，并以数据库状态作为最终判定。

        参数:
            approval_id (str): 要等待的审批 ID。
            timeout (float): 本次等待允许使用的最长秒数。
        返回值:
            bool: 只有明确批准时返回 ``True``；拒绝、取消、过期均返回 ``False``。
        异常:
            asyncio.CancelledError: 外层运行被取消时向上传播，并将审批过期化。
            数据库异常: 读取或更新审批状态失败时传播。
        """
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[AgentApprovalDecision]
        record = None
        async with self._waiters_lock:
            record = await self._agent_store.get_approval(approval_id)
            if record is not None:
                decision = self._record_decision(record)
                if decision is not None:
                    self._remember_record_decision(approval_id, record, decision)
                    return decision == AgentApprovalDecision.APPROVED
            waiter = self._waiters.get(approval_id)
            if waiter is None:
                waiter = loop.create_future()
                self._waiters[approval_id] = waiter

            # 响应接口先提交 DB、后尝试唤醒 Future。二次检查覆盖“响应发生在
            # Future 注册前”的竞态，也兼容进程重启后当前进程没有 Future 的情况。
            record = await self._agent_store.get_approval(approval_id)
            if record is not None:
                decision = self._record_decision(record)
                if decision is not None and not waiter.done():
                    waiter.set_result(decision)

        wait_timeout = self._remaining_timeout(record, timeout)
        try:
            decision = await asyncio.wait_for(
                asyncio.shield(waiter), timeout=wait_timeout
            )
            return decision == AgentApprovalDecision.APPROVED
        except asyncio.CancelledError:
            await self.expire_approval(approval_id)
            raise
        except asyncio.TimeoutError:
            # 超时和响应可能同时发生。DB 原子更新完成后，以 DB 再读结果为准，
            # 防止一个已经批准的请求被本地 timeout 误判为拒绝。
            await self.expire_approval(approval_id)
            latest = await self._agent_store.get_approval(approval_id)
            decision = self._record_decision(latest) if latest is not None else None
            return decision == AgentApprovalDecision.APPROVED
        finally:
            async with self._waiters_lock:
                current = self._waiters.get(approval_id)
                if current is waiter and current.done():
                    self._waiters.pop(approval_id, None)

    async def expire_approval(self, approval_id: str) -> bool:
        """将仍待处理的审批标记为过期并发布事件。"""
        before = await self._agent_store.get_approval(approval_id)
        resolved = await self._agent_store.resolve_approval(
            approval_id, AgentApprovalDecision.EXPIRED
        )
        if not resolved:
            latest = await self._agent_store.get_approval(approval_id)
            decision = self._record_decision(latest) if latest is not None else None
            if decision is not None:
                await self._wake(approval_id, decision)
            return False
        record = before or await self._agent_store.get_approval(approval_id)
        if record is not None:
            request = self._requests.get(approval_id)
            if request is None:
                request = self._request_for_record(record)
            self._mark_request(request, AgentApprovalDecision.EXPIRED)
        await self._wake(approval_id, AgentApprovalDecision.EXPIRED)
        if record is not None:
            await self._publish_resolution_event(request, AgentApprovalDecision.EXPIRED)
        return True

    async def submit_approval_decision(self, approval_id: str, action: str) -> bool:
        """原子写入外部审批决定，并唤醒当前进程中的等待 Future。

        参数:
            approval_id (str): 审批 ID。
            action (str): ``allow``/``approve``/``approved``、``cancel`` 或拒绝动作。
        返回值:
            bool: 本次确实从 pending 转换成功时返回 ``True``。
        异常:
            数据库或事件发布异常时传播。
        """
        normalized = action.lower()
        if normalized in {"allow", "approve", "approved"}:
            decision = AgentApprovalDecision.APPROVED
        elif normalized in {"cancel", "cancelled", "canceled"}:
            decision = AgentApprovalDecision.CANCELLED
        else:
            decision = AgentApprovalDecision.DENIED

        record = await self._agent_store.get_approval(approval_id)
        resolved = await self._agent_store.resolve_approval(approval_id, decision)
        if not resolved:
            latest = await self._agent_store.get_approval(approval_id)
            if latest is not None:
                existing_decision = self._record_decision(latest)
                if existing_decision is not None:
                    await self._wake(approval_id, existing_decision)
            return False
        if record is not None:
            request = self._requests.get(approval_id)
            if request is None:
                request = self._request_for_record(record)
            self._mark_request(request, decision)
        await self._wake(approval_id, decision)
        if record is not None:
            await self._publish_resolution_event(request, decision)
        return True

    async def _publish_resolution_event(
        self, request: ApprovalRequest, decision: AgentApprovalDecision
    ) -> None:
        """发布审批终态事件，即使当前进程刚刚从数据库恢复。"""
        await self._event_publisher.publish(
            ApplicationEvent(
                event_type=(
                    EventType.APPROVAL_EXPIRED
                    if decision == AgentApprovalDecision.EXPIRED
                    else EventType.APPROVAL_RESOLVED
                ),
                durability=EventDurability.DURABLE,
                session_id=request.session_id,
                run_id=request.run_id,
                payload={
                    "approval_id": request.id,
                    "tool_call_id": request.tool_call_id,
                    "decision": decision.value,
                },
            )
        )

    async def _wake(self, approval_id: str, decision: AgentApprovalDecision) -> None:
        """完成当前进程中指定审批的 Future。"""
        async with self._waiters_lock:
            waiter = self._waiters.get(approval_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(decision)

    async def cancel_approval(self, approval_id: str) -> bool:
        """取消一个审批请求，并写入 ``cancelled`` 决定。"""
        return await self.submit_approval_decision(approval_id, "cancel")

    async def cancel_pending_approvals(self, session_id: str) -> None:
        """从数据库扫描并取消指定会话的全部待审批记录。

        参数:
            session_id (str): 会话 ID；空字符串表示全部会话。
        返回值:
            None: 所有匹配记录均已尝试取消。
        异常:
            存储或事件发布失败时传播。
        """
        pending = getattr(self._agent_store, "list_pending_approvals", None)
        if pending is not None:
            rows = await pending(session_id or None)
            approval_ids = [row.approval_id for row in rows]
        else:
            approval_ids = [
                request.id
                for request in self._requests.values()
                if not session_id or request.session_id == session_id
            ]
        for approval_id in approval_ids:
            await self.cancel_approval(approval_id)

    def list_pending_approvals(
        self, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """返回当前进程已索引的未解决审批请求。

        参数:
            session_id (str | None): 可选会话过滤条件。
        返回值:
            list[dict[str, Any]]: 面向兼容调用方的审批请求字典列表。
        异常:
            不抛出业务异常。
        """
        requests = [
            request
            for request in self._requests.values()
            if not request.resolved
            and (not session_id or request.session_id == session_id)
        ]
        return [
            {
                "approval_id": request.id,
                "tool_name": request.tool_name,
                "arguments": request.arguments,
                "risk_level": request.risk_level,
                "created_at": request.created_at.isoformat(),
                "expires_at": (
                    request.expires_at.isoformat() if request.expires_at else None
                ),
                "session_id": request.session_id,
                "run_id": request.run_id,
                "tool_call_id": request.tool_call_id,
                "resolved": request.resolved,
                "resolution": request.resolution,
            }
            for request in requests
        ]

    def _request_for_record(self, record: Any) -> ApprovalRequest:
        """把 ORM 审批记录转换为运行时展示对象。"""
        request = self._request_from_record(record)
        self._requests[request.id] = request
        return request

    @staticmethod
    def _request_from_record(record: Any) -> ApprovalRequest:
        """从数据库行恢复审批请求的展示字段和原始截止时间。"""
        created_at = _parse_datetime(getattr(record, "created_at", None))
        expires_at = _parse_optional_datetime(getattr(record, "expires_at", None))
        duration = (
            max(0, int((expires_at - created_at).total_seconds()))
            if expires_at is not None
            else 0
        )
        request = ApprovalRequest(
            id=record.approval_id,
            tool_name=record.tool_name,
            arguments=_json_object(getattr(record, "arguments_json", "{}")),
            risk_level=record.risk_level,
            timeout=duration,
            created_at=created_at,
            session_id=record.session_id,
            run_id=record.run_id,
            tool_call_id=record.tool_call_id,
            expires_at=expires_at,
        )
        decision = _decision_from_value(getattr(record, "decision", None))
        if decision is not None:
            request.resolved = True
            request.resolution = ApprovalManager._request_resolution(decision)
            request.decided_at = _parse_optional_datetime(
                getattr(record, "decided_at", None)
            )
        return request

    @staticmethod
    def _request_resolution(decision: AgentApprovalDecision) -> str:
        """将 Runtime 决定映射到展示模型的 resolution 字段。"""
        if decision == AgentApprovalDecision.EXPIRED:
            return ApprovalDecision.TIMEOUT.value
        return decision.value

    @staticmethod
    def _record_decision(record: Any) -> AgentApprovalDecision | None:
        """读取已解析记录的标准决定；pending 返回 ``None``。"""
        if (
            record is None
            or getattr(record, "status", None) == AgentApprovalStatus.PENDING.value
        ):
            return None
        return (
            _decision_from_value(getattr(record, "decision", None))
            or AgentApprovalDecision.DENIED
        )

    def _remember_record_decision(
        self,
        approval_id: str,
        record: Any,
        decision: AgentApprovalDecision,
    ) -> None:
        """把 DB 中恢复出的终态同步到进程内索引。"""
        request = self._request_for_record(record)
        request.id = approval_id
        self._mark_request(request, decision)

    @staticmethod
    def _mark_request(
        request: ApprovalRequest, decision: AgentApprovalDecision
    ) -> None:
        """更新运行时审批对象的终态字段。"""
        request.resolved = True
        request.resolution = ApprovalManager._request_resolution(decision)
        request.decided_at = datetime.now(timezone.utc)

    @staticmethod
    def _remaining_timeout(record: Any, fallback: float) -> float:
        """根据持久化截止时间计算本次等待剩余时间。"""
        expires_at = _parse_optional_datetime(getattr(record, "expires_at", None))
        if expires_at is None:
            return max(0.0, fallback)
        remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, min(fallback, remaining))


def _parse_datetime(value: Any) -> datetime:
    """解析 SQLite 中的 ISO 时间，缺失时使用当前 UTC 时间。"""
    parsed = _parse_optional_datetime(value)
    return parsed or datetime.now(timezone.utc)


def _parse_optional_datetime(value: Any) -> datetime | None:
    """解析可空的 ISO 时间并补齐 UTC 时区。"""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return (
            parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        )
    return None


def _json_object(value: Any) -> dict[str, Any]:
    """将审批参数 JSON 文本转换为对象。"""
    import json

    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _decision_from_value(value: Any) -> AgentApprovalDecision | None:
    """把数据库决定转换为共享枚举。"""
    if value is None:
        return None
    try:
        return AgentApprovalDecision(str(value))
    except ValueError:
        return AgentApprovalDecision.DENIED
