"""Repository 层 — 封装各实体的 CRUD 操作.

采用 Repository 模式，每个实体对应一个 Repository 类。
查询方法返回类型化领域模型（athena.models），save 方法接受类型化模型。
所有删除操作为软删除（设置 deleted_time）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from athena.db.engine import get_session
from athena.db.models import (
    ApprovalLogModel,
    Base,
    MessageModel,
    SessionModel,
    StepModel,
    ToolCallModel,
)
from athena.models import (
    ApprovalDecision,
    ApprovalLog,
    Message,
    MessageRole,
    Session,
    SessionStatus,
    Step,
    StepStatus,
    StepType,
    ToolCallRecord,
    ToolCallStatus,
)
from athena.utils.logging import get_logger

logger = get_logger(__name__)

# 用于区分"未传参"和"显式传 None"的哨兵对象
_SENTINEL = object()


def _now_iso() -> str:
    """返回当前时间的 ISO 格式字符串."""
    return datetime.now().isoformat()


def _json_dumps(value: Any) -> str:
    """JSON 序列化，支持自定义类型."""
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_loads(value: str | None, default: Any) -> Any:
    """JSON 反序列化，失败返回默认值."""
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


# ---------------------------------------------------------------------------
# ORM row → 领域模型 转换（集中查询结果的 dict 构建逻辑）
# ---------------------------------------------------------------------------


def _row_to_session(row: SessionModel) -> Session:
    return Session(
        id=row.id,
        title=row.title,
        status=SessionStatus(row.status),
        run_id=row.run_id,
        created_at=datetime.fromisoformat(row.created_at),
        updated_at=datetime.fromisoformat(row.updated_at),
        metadata=_json_loads(row.metadata_json, {}),
        compression_summary=row.compression_summary,
        last_compressed_message_id=row.last_compressed_message_id,
        last_summarized_message_id=row.last_summarized_message_id,
    )


def _row_to_message(row: MessageModel) -> Message:
    return Message(
        id=row.id,
        session_id=row.session_id,
        role=MessageRole(row.role),
        content=row.content,
        tool_calls=_json_loads(row.tool_calls_json, []),
        tool_call_id=row.tool_call_id,
        run_id=row.run_id,
        metadata=_json_loads(row.metadata_json, {}),
        timestamp=datetime.fromisoformat(row.timestamp),
    )


def _row_to_step(row: StepModel) -> Step:
    return Step(
        id=row.id,
        session_id=row.session_id,
        run_id=row.run_id,
        step_number=row.step_number,
        step_type=StepType(row.step_type),
        parent_step_id=row.parent_step_id,
        parent_run_id=row.parent_run_id,
        status=StepStatus(row.status),
        started_at=datetime.fromisoformat(row.started_at),
        completed_at=datetime.fromisoformat(row.completed_at) if row.completed_at else None,
        duration_ms=row.duration_ms,
        llm_input_tokens=row.llm_input_tokens,
        llm_output_tokens=row.llm_output_tokens,
        error_message=row.error_message,
        metadata=_json_loads(row.metadata_json, {}),
    )


def _row_to_tool_call(row: ToolCallModel) -> ToolCallRecord:
    return ToolCallRecord(
        id=row.id,
        session_id=row.session_id,
        step_id=row.step_id,
        tool_name=row.tool_name,
        arguments=_json_loads(row.arguments_json, {}),
        raw_output=row.raw_output,
        status=ToolCallStatus(row.status),
        started_at=datetime.fromisoformat(row.started_at),
        completed_at=datetime.fromisoformat(row.completed_at) if row.completed_at else None,
        duration_ms=row.duration_ms,
        error_message=row.error_message,
        error_stack=row.error_stack,
    )


def _row_to_approval_log(row: ApprovalLogModel) -> ApprovalLog:
    return ApprovalLog(
        id=row.id,
        session_id=row.session_id,
        tool_call_id=row.tool_call_id,
        tool_name=row.tool_name,
        arguments=_json_loads(row.arguments_json, {}),
        risk_level=row.risk_level,
        decision=ApprovalDecision(row.decision),
        decision_time_ms=row.decision_time_ms,
        timestamp=datetime.fromisoformat(row.timestamp),
    )


# ---------------------------------------------------------------------------
# SessionRepository
# ---------------------------------------------------------------------------


class SessionRepository:
    """会话表 CRUD 操作."""

    async def create(self, session_id: str, title: str = "New Session") -> Session:
        """创建新会话."""
        now = datetime.now()
        async with get_session() as session:
            async with session.begin():
                model = SessionModel(
                    id=session_id,
                    title=title,
                    status=SessionStatus.IDLE.value,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                    metadata_json="{}",
                )
                session.add(model)
            return Session(
                id=session_id,
                title=title,
                status=SessionStatus.IDLE,
                created_at=now,
                updated_at=now,
                metadata={},
            )

    async def get(self, session_id: str, include_deleted: bool = False) -> Session | None:
        """获取单个会话."""
        async with get_session() as session:
            stmt = select(SessionModel).where(SessionModel.id == session_id)
            if not include_deleted:
                stmt = stmt.where(SessionModel.deleted_time.is_(None))
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return _row_to_session(row)

    async def list_all(self, include_deleted: bool = False) -> list[Session]:
        """列出所有会话."""
        async with get_session() as session:
            stmt = select(SessionModel).order_by(SessionModel.updated_at.desc())
            if not include_deleted:
                stmt = stmt.where(SessionModel.deleted_time.is_(None))
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [_row_to_session(row) for row in rows]

    async def update(
        self,
        session_id: str,
        *,
        status: str | None = None,
        run_id: str | None = None,
        title: str | None = None,
        compression_summary: str | None = _SENTINEL,
        last_compressed_message_id: str | None = _SENTINEL,
        last_summarized_message_id: str | None = _SENTINEL,
    ) -> None:
        """更新会话字段.

        Args:
            compression_summary: 摘要缓冲区文本（None 表示清空，_SENTINEL 表示不更新）
            last_compressed_message_id: 上次压缩的最后一条消息 ID
            last_summarized_message_id: 上次阈值摘要的最后一条消息 ID
        """
        values: dict[str, Any] = {"updated_at": _now_iso()}
        if status is not None:
            values["status"] = status
        if run_id is not None:
            values["run_id"] = run_id
        if title is not None:
            values["title"] = title
        if compression_summary is not _SENTINEL:
            values["compression_summary"] = compression_summary
        if last_compressed_message_id is not _SENTINEL:
            values["last_compressed_message_id"] = last_compressed_message_id
        if last_summarized_message_id is not _SENTINEL:
            values["last_summarized_message_id"] = last_summarized_message_id

        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(SessionModel)
                    .where(SessionModel.id == session_id, SessionModel.deleted_time.is_(None))
                    .values(**values)
                )

    async def query_by_status(self, status_list: list[str]) -> list[Session]:
        """按状态查询会话."""
        async with get_session() as session:
            stmt = (
                select(SessionModel)
                .where(
                    SessionModel.status.in_(status_list),
                    SessionModel.deleted_time.is_(None),
                )
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [_row_to_session(row) for row in rows]

    async def delete(self, session_id: str) -> None:
        """软删除会话及其所有关联数据."""
        now = _now_iso()
        async with get_session() as session:
            async with session.begin():
                # 软删除 session 本身
                await session.execute(
                    update(SessionModel)
                    .where(SessionModel.id == session_id, SessionModel.deleted_time.is_(None))
                    .values(deleted_time=now)
                )
                # 级联软删除关联数据
                await session.execute(
                    update(MessageModel)
                    .where(MessageModel.session_id == session_id, MessageModel.deleted_time.is_(None))
                    .values(deleted_time=now)
                )
                await session.execute(
                    update(StepModel)
                    .where(StepModel.session_id == session_id, StepModel.deleted_time.is_(None))
                    .values(deleted_time=now)
                )
                await session.execute(
                    update(ToolCallModel)
                    .where(ToolCallModel.session_id == session_id, ToolCallModel.deleted_time.is_(None))
                    .values(deleted_time=now)
                )
                await session.execute(
                    update(ApprovalLogModel)
                    .where(ApprovalLogModel.session_id == session_id, ApprovalLogModel.deleted_time.is_(None))
                    .values(deleted_time=now)
                )


# ---------------------------------------------------------------------------
# MessageRepository
# ---------------------------------------------------------------------------


class MessageRepository:
    """消息表 CRUD 操作."""

    async def save(self, message: Message) -> str:
        """保存消息并同步刷新会话的 updated_at（同一事务）."""
        async with get_session() as session:
            async with session.begin():
                model = MessageModel(
                    id=message.id,
                    session_id=message.session_id,
                    role=message.role.value,
                    content=message.content,
                    tool_calls_json=_json_dumps(message.tool_calls),
                    tool_call_id=message.tool_call_id,
                    run_id=message.run_id,
                    metadata_json=_json_dumps(message.metadata),
                    timestamp=message.timestamp.isoformat(),
                )
                session.add(model)
                # 在同一事务内刷新会话时间戳，避免额外的 DB 往返
                await session.execute(
                    update(SessionModel)
                    .where(SessionModel.id == message.session_id, SessionModel.deleted_time.is_(None))
                    .values(updated_at=_now_iso())
                )
            return message.id

    async def get_by_session(
        self, session_id: str, limit: int | None = None, include_deleted: bool = False
    ) -> list[Message]:
        """获取会话的消息列表."""
        async with get_session() as session:
            stmt = (
                select(MessageModel)
                .where(MessageModel.session_id == session_id)
                .order_by(MessageModel.timestamp.asc())
            )
            if not include_deleted:
                stmt = stmt.where(MessageModel.deleted_time.is_(None))
            if limit:
                stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [_row_to_message(row) for row in rows]

    async def get_after_message(
        self, session_id: str, after_id: str, include_deleted: bool = False
    ) -> list[Message]:
        """获取指定消息之后的消息列表（用于增量压缩）.

        消息 ID 采用 generate_time_id() 生成（微秒级时间戳，单调递增），
        因此可直接通过字符串比较实现时序过滤。
        """
        async with get_session() as session:
            stmt = (
                select(MessageModel)
                .where(
                    MessageModel.session_id == session_id,
                    MessageModel.id > after_id,  # ID 单调递增，字符串比较即可
                )
                .order_by(MessageModel.id.asc())
            )
            if not include_deleted:
                stmt = stmt.where(MessageModel.deleted_time.is_(None))
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [_row_to_message(row) for row in rows]


# ---------------------------------------------------------------------------
# StepRepository
# ---------------------------------------------------------------------------


class StepRepository:
    """执行步骤表 CRUD 操作."""

    async def save(self, step: Step) -> None:
        """保存执行步骤."""
        async with get_session() as session:
            async with session.begin():
                model = StepModel(
                    id=step.id,
                    session_id=step.session_id,
                    run_id=step.run_id,
                    step_number=step.step_number,
                    step_type=step.step_type.value,
                    parent_step_id=step.parent_step_id,
                    parent_run_id=step.parent_run_id,
                    status=step.status.value,
                    started_at=step.started_at.isoformat(),
                    completed_at=step.completed_at.isoformat() if step.completed_at else None,
                    duration_ms=step.duration_ms,
                    llm_input_tokens=step.llm_input_tokens,
                    llm_output_tokens=step.llm_output_tokens,
                    error_message=step.error_message,
                    metadata_json=_json_dumps(step.metadata),
                )
                session.add(model)

    async def update(self, step_id: str, updates: dict[str, Any]) -> None:
        """更新执行步骤."""
        if not updates:
            return
        allowed = {
            "status", "completed_at", "duration_ms", "llm_input_tokens",
            "llm_output_tokens", "error_message", "metadata",
        }
        values: dict[str, Any] = {}
        for key, value in updates.items():
            if key not in allowed:
                continue
            # ORM 列名为 metadata_json，外部 API 使用 "metadata"
            if key == "metadata":
                value = _json_dumps(value)
                values["metadata_json"] = value
            else:
                values[key] = value
        if not values:
            return

        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(StepModel)
                    .where(StepModel.id == step_id, StepModel.deleted_time.is_(None))
                    .values(**values)
                )

    async def update_running_by_session(
        self, session_id: str, updates: dict[str, Any]
    ) -> None:
        """批量更新指定会话所有 running 步骤（进程中断恢复清理）."""
        if not updates:
            return
        allowed = {
            "status", "completed_at", "duration_ms", "llm_input_tokens",
            "llm_output_tokens", "error_message", "metadata",
        }
        values: dict[str, Any] = {}
        for key, value in updates.items():
            if key not in allowed:
                continue
            # ORM 列名为 metadata_json，外部 API 使用 "metadata"
            if key == "metadata":
                value = _json_dumps(value)
                values["metadata_json"] = value
            else:
                values[key] = value
        if not values:
            return

        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(StepModel)
                    .where(
                        StepModel.session_id == session_id,
                        StepModel.status == "running",
                        StepModel.deleted_time.is_(None),
                    )
                    .values(**values)
                )

    async def get_by_session(self, session_id: str, include_deleted: bool = False) -> list[Step]:
        """获取会话的执行步骤."""
        async with get_session() as session:
            stmt = (
                select(StepModel)
                .where(StepModel.session_id == session_id)
                .order_by(StepModel.step_number.asc())
            )
            if not include_deleted:
                stmt = stmt.where(StepModel.deleted_time.is_(None))
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [_row_to_step(row) for row in rows]

    async def get_by_run(self, run_id: str, include_deleted: bool = False) -> list[Step]:
        """按 run_id 获取执行步骤."""
        async with get_session() as session:
            stmt = (
                select(StepModel)
                .where(StepModel.run_id == run_id)
                .order_by(StepModel.step_number.asc())
            )
            if not include_deleted:
                stmt = stmt.where(StepModel.deleted_time.is_(None))
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [_row_to_step(row) for row in rows]

    async def get_last_step_number(self, run_id: str) -> int:
        """获取指定 run_id 的最大步骤号."""
        from sqlalchemy import func

        async with get_session() as session:
            stmt = (
                select(func.max(StepModel.step_number))
                .where(StepModel.run_id == run_id, StepModel.deleted_time.is_(None))
            )
            result = await session.execute(stmt)
            max_num = result.scalar()
            return max_num if max_num is not None else 0


# ---------------------------------------------------------------------------
# ToolCallRepository
# ---------------------------------------------------------------------------


class ToolCallRepository:
    """工具调用记录表 CRUD 操作."""

    async def save(self, tool_call: ToolCallRecord) -> None:
        """保存工具调用记录."""
        async with get_session() as session:
            async with session.begin():
                model = ToolCallModel(
                    id=tool_call.id,
                    session_id=tool_call.session_id,
                    step_id=tool_call.step_id,
                    tool_name=tool_call.tool_name,
                    arguments_json=_json_dumps(tool_call.arguments),
                    raw_output=tool_call.raw_output,
                    status=tool_call.status.value,
                    started_at=tool_call.started_at.isoformat(),
                    completed_at=tool_call.completed_at.isoformat() if tool_call.completed_at else None,
                    duration_ms=tool_call.duration_ms,
                    error_message=tool_call.error_message,
                    error_stack=tool_call.error_stack,
                )
                session.add(model)

    async def update(self, tool_call_id: str, updates: dict[str, Any]) -> None:
        """更新工具调用记录."""
        if not updates:
            return
        allowed = {
            "raw_output", "status", "completed_at", "duration_ms",
            "error_message", "error_stack",
        }
        values: dict[str, Any] = {}
        for key, value in updates.items():
            if key not in allowed:
                continue
            values[key] = value
        if not values:
            return

        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolCallModel)
                    .where(ToolCallModel.id == tool_call_id, ToolCallModel.deleted_time.is_(None))
                    .values(**values)
                )

    async def update_running_by_session(
        self, session_id: str, updates: dict[str, Any]
    ) -> None:
        """批量更新指定会话所有 running 工具调用（进程中断恢复清理）."""
        if not updates:
            return
        allowed = {
            "raw_output", "status", "completed_at", "duration_ms",
            "error_message", "error_stack",
        }
        values: dict[str, Any] = {}
        for key, value in updates.items():
            if key not in allowed:
                continue
            values[key] = value
        if not values:
            return

        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolCallModel)
                    .where(
                        ToolCallModel.session_id == session_id,
                        ToolCallModel.status == "running",
                        ToolCallModel.deleted_time.is_(None),
                    )
                    .values(**values)
                )

    async def query(
        self, session_id: str, status: str | None = None, include_deleted: bool = False
    ) -> list[ToolCallRecord]:
        """查询工具调用记录."""
        async with get_session() as session:
            stmt = (
                select(ToolCallModel)
                .where(ToolCallModel.session_id == session_id)
                .order_by(ToolCallModel.started_at.asc())
            )
            if not include_deleted:
                stmt = stmt.where(ToolCallModel.deleted_time.is_(None))
            if status:
                stmt = stmt.where(ToolCallModel.status == status)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [_row_to_tool_call(row) for row in rows]

    async def last_called_by_tool(self) -> dict[str, str]:
        """返回 工具名 -> 最近调用时间(ISO) 的映射，用于工具管理页「最近调用」列."""
        async with get_session() as session:
            stmt = (
                select(ToolCallModel.tool_name, func.max(ToolCallModel.started_at))
                .where(ToolCallModel.deleted_time.is_(None))
                .group_by(ToolCallModel.tool_name)
            )
            result = await session.execute(stmt)
            return {name: last for name, last in result.all()}

    async def count_calls_since(self, since: datetime) -> int:
        """统计指定时间点之后的工具调用次数（含所有状态）."""
        async with get_session() as session:
            stmt = (
                select(func.count(ToolCallModel.id))
                .where(
                    ToolCallModel.started_at >= since.isoformat(),
                    ToolCallModel.deleted_time.is_(None),
                )
            )
            result = await session.execute(stmt)
            return int(result.scalar_one())


# ---------------------------------------------------------------------------
# ApprovalLogRepository
# ---------------------------------------------------------------------------


class ApprovalLogRepository:
    """审批日志表 CRUD 操作."""

    async def save(self, log: ApprovalLog) -> None:
        """保存审批日志."""
        async with get_session() as session:
            async with session.begin():
                model = ApprovalLogModel(
                    id=log.id,
                    session_id=log.session_id,
                    tool_call_id=log.tool_call_id,
                    tool_name=log.tool_name,
                    arguments_json=_json_dumps(log.arguments),
                    risk_level=log.risk_level,
                    decision=log.decision.value,
                    decision_time_ms=log.decision_time_ms,
                    timestamp=log.timestamp.isoformat(),
                )
                session.add(model)

    async def get_by_session(
        self, session_id: str, include_deleted: bool = False
    ) -> list[ApprovalLog]:
        """获取会话的审批日志."""
        async with get_session() as session:
            stmt = (
                select(ApprovalLogModel)
                .where(ApprovalLogModel.session_id == session_id)
                .order_by(ApprovalLogModel.timestamp.asc())
            )
            if not include_deleted:
                stmt = stmt.where(ApprovalLogModel.deleted_time.is_(None))
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [_row_to_approval_log(row) for row in rows]

    async def query_by_tool_call(
        self, tool_call_id: str, include_deleted: bool = False
    ) -> ApprovalLog | None:
        """按 tool_call_id 查询审批日志."""
        async with get_session() as session:
            stmt = select(ApprovalLogModel).where(
                ApprovalLogModel.tool_call_id == tool_call_id
            )
            if not include_deleted:
                stmt = stmt.where(ApprovalLogModel.deleted_time.is_(None))
            stmt = stmt.limit(1)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return _row_to_approval_log(row)

    async def list_all(
        self, limit: int = 50, offset: int = 0, session_id: str | None = None
    ) -> list[ApprovalLog]:
        """分页列出审批日志，按时间倒序（新→旧），可选按会话过滤."""
        async with get_session() as session:
            stmt = select(ApprovalLogModel).where(
                ApprovalLogModel.deleted_time.is_(None)
            )
            if session_id:
                stmt = stmt.where(ApprovalLogModel.session_id == session_id)
            stmt = (
                stmt.order_by(ApprovalLogModel.timestamp.desc())
                .offset(offset)
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [_row_to_approval_log(row) for row in rows]

    async def stats(self) -> dict[str, int]:
        """统计今日审批决策数，返回 {today_total, today_approved, today_denied, today_timeout}."""
        today_start = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        async with get_session() as session:
            stmt = (
                select(ApprovalLogModel.decision, func.count(ApprovalLogModel.id))
                .where(
                    ApprovalLogModel.deleted_time.is_(None),
                    ApprovalLogModel.timestamp >= today_start.isoformat(),
                )
                .group_by(ApprovalLogModel.decision)
            )
            result = await session.execute(stmt)
            counts = {decision: count for decision, count in result.all()}
            total = sum(counts.values())
            return {
                "today_total": total,
                "today_approved": counts.get("approved", 0),
                "today_denied": counts.get("denied", 0),
                "today_timeout": counts.get("timeout", 0),
            }
