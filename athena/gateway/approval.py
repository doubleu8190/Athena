"""持久化审批服务。PostgreSQL 记录是审批状态的唯一事实来源。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from athena.contracts.events import ApplicationEvent, EventDurability, EventType
from athena.contracts.ports import AgentStorePort
from athena.models.approval import ApprovalRequest
from athena.models.tool import RiskLevel
from athena.runtime.transport import RuntimeEventPublisher
from athena.utils.id_generation import generate_time_id


class ApprovalManager:
    """协调审批请求的持久化和事件通知。"""

    def __init__(
        self,
        event_publisher: RuntimeEventPublisher,
        agent_store: AgentStorePort,
    ) -> None:
        """创建审批管理器。

        参数:
            event_publisher (RuntimeEventPublisher): 应用事件发布器。
            agent_store (AgentStore): 审批状态的持久化存储。
        返回值:
            None。
        异常:
            不抛出业务异常。
        """
        self._event_publisher = event_publisher
        self._agent_store = agent_store

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
        approval_batch_id: str | None = None,
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
        if record is not None:
            return self._request_from_record(record)

        created_at = datetime.now(timezone.utc)
        generated_id = approval_id or generate_time_id()
        request = ApprovalRequest(
            id=generated_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            risk_level=risk_level.value,
            created_at=created_at,
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
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
            approval_batch_id=approval_batch_id,
        )
        if created is False:
            # 另一个恢复执行者已经建立了记录；只复用它，不再重复发事件。
            existing = await self._agent_store.get_approval(generated_id)
            if existing is None:
                raise RuntimeError("approval was not created and cannot be recovered")
            return self._request_from_record(existing)

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
                    "approval_batch_id": approval_batch_id,
                    "tool_call_id": tool_call_id,
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "worker_run_id": worker_run_id,
                },
            )
        )
        return request

    @staticmethod
    def _request_from_record(record: Any) -> ApprovalRequest:
        """从数据库行恢复审批请求的展示字段。"""
        created_at = _parse_datetime(getattr(record, "created_at", None))
        request = ApprovalRequest(
            id=record.approval_id,
            tool_name=record.tool_name,
            arguments=_json_object(getattr(record, "arguments_json", "{}")),
            risk_level=record.risk_level,
            created_at=created_at,
            session_id=record.session_id,
            run_id=record.run_id,
            tool_call_id=record.tool_call_id,
        )
        return request


def _parse_datetime(value: Any) -> datetime:
    """解析 PostgreSQL 中的 ISO 时间，缺失时使用当前 UTC 时间。"""
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
