"""Repository 层 — 封装各实体的 CRUD 操作.

采用 Repository 模式，每个实体对应一个 Repository 类。
所有方法接受/返回 dict，保持与旧 Database 类的 API 兼容。
所有删除操作为软删除（设置 deleted_time）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
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
from athena.utils.ids import generate_time_id
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
# SessionRepository
# ---------------------------------------------------------------------------


class SessionRepository:
    """会话表 CRUD 操作."""

    async def create(self, session_id: str, title: str = "New Session") -> dict[str, Any]:
        """创建新会话."""
        now = _now_iso()
        async with get_session() as session:
            async with session.begin():
                model = SessionModel(
                    id=session_id,
                    title=title,
                    status="idle",
                    created_at=now,
                    updated_at=now,
                    metadata_json="{}",
                )
                session.add(model)
            return {
                "id": session_id,
                "title": title,
                "status": "idle",
                "created_at": now,
                "updated_at": now,
                "metadata": {},
            }

    async def get(self, session_id: str, include_deleted: bool = False) -> dict[str, Any] | None:
        """获取单个会话."""
        async with get_session() as session:
            stmt = select(SessionModel).where(SessionModel.id == session_id)
            if not include_deleted:
                stmt = stmt.where(SessionModel.deleted_time.is_(None))
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return {
                "id": row.id,
                "title": row.title,
                "status": row.status,
                "run_id": row.run_id,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "metadata": _json_loads(row.metadata_json, {}),
                "compression_summary": row.compression_summary,
                "last_compressed_message_id": row.last_compressed_message_id,
            }

    async def list_all(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        """列出所有会话."""
        async with get_session() as session:
            stmt = select(SessionModel).order_by(SessionModel.updated_at.desc())
            if not include_deleted:
                stmt = stmt.where(SessionModel.deleted_time.is_(None))
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [
                {
                    "id": row.id,
                    "title": row.title,
                    "status": row.status,
                    "run_id": row.run_id,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                    "metadata": _json_loads(row.metadata_json, {}),
                    "compression_summary": row.compression_summary,
                    "last_compressed_message_id": row.last_compressed_message_id,
                }
                for row in rows
            ]

    async def update(
        self,
        session_id: str,
        *,
        status: str | None = None,
        run_id: str | None = None,
        title: str | None = None,
        compression_summary: str | None = _SENTINEL,
        last_compressed_message_id: str | None = _SENTINEL,
    ) -> None:
        """更新会话字段.

        Args:
            compression_summary: 摘要缓冲区文本（None 表示清空，_SENTINEL 表示不更新）
            last_compressed_message_id: 上次压缩的最后一条消息 ID
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

        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(SessionModel)
                    .where(SessionModel.id == session_id, SessionModel.deleted_time.is_(None))
                    .values(**values)
                )

    async def query_by_status(self, status_list: list[str]) -> list[dict[str, Any]]:
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
            return [
                {
                    "id": row.id,
                    "title": row.title,
                    "status": row.status,
                    "run_id": row.run_id,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                    "metadata": _json_loads(row.metadata_json, {}),
                }
                for row in rows
            ]

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

    async def save(self, session_id: str, message: dict[str, Any]) -> str:
        """保存消息."""
        msg_id = message.get("id") or generate_time_id()
        async with get_session() as session:
            async with session.begin():
                model = MessageModel(
                    id=msg_id,
                    session_id=session_id,
                    role=message["role"],
                    content=message.get("content", ""),
                    tool_calls_json=_json_dumps(message.get("tool_calls", [])),
                    tool_call_id=message.get("tool_call_id"),
                    metadata_json=_json_dumps(message.get("metadata", {})),
                    timestamp=message.get("timestamp", _now_iso()),
                )
                session.add(model)
            return msg_id

    async def get_by_session(
        self, session_id: str, limit: int | None = None, include_deleted: bool = False
    ) -> list[dict[str, Any]]:
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
            return [
                {
                    "id": row.id,
                    "session_id": row.session_id,
                    "role": row.role,
                    "content": row.content,
                    "tool_calls": _json_loads(row.tool_calls_json, []),
                    "tool_call_id": row.tool_call_id,
                    "metadata": _json_loads(row.metadata_json, {}),
                    "timestamp": row.timestamp,
                }
                for row in rows
            ]

    async def get_after_message(
        self, session_id: str, after_id: str, include_deleted: bool = False
    ) -> list[dict[str, Any]]:
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
            return [
                {
                    "id": row.id,
                    "session_id": row.session_id,
                    "role": row.role,
                    "content": row.content,
                    "tool_calls": _json_loads(row.tool_calls_json, []),
                    "tool_call_id": row.tool_call_id,
                    "metadata": _json_loads(row.metadata_json, {}),
                    "timestamp": row.timestamp,
                }
                for row in rows
            ]


# ---------------------------------------------------------------------------
# StepRepository
# ---------------------------------------------------------------------------


class StepRepository:
    """执行步骤表 CRUD 操作."""

    async def save(self, step: dict[str, Any]) -> None:
        """保存执行步骤."""
        async with get_session() as session:
            async with session.begin():
                model = StepModel(
                    id=step["id"],
                    session_id=step["session_id"],
                    run_id=step["run_id"],
                    step_number=step["step_number"],
                    step_type=step["step_type"],
                    parent_step_id=step.get("parent_step_id"),
                    status=step.get("status", "pending"),
                    started_at=step["started_at"],
                    completed_at=step.get("completed_at"),
                    duration_ms=step.get("duration_ms", 0),
                    llm_input_tokens=step.get("llm_input_tokens", 0),
                    llm_output_tokens=step.get("llm_output_tokens", 0),
                    error_message=step.get("error_message"),
                    metadata_json=_json_dumps(step.get("metadata", {})),
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

    async def get_by_session(self, session_id: str, include_deleted: bool = False) -> list[dict[str, Any]]:
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
            return [
                {
                    "id": row.id,
                    "session_id": row.session_id,
                    "run_id": row.run_id,
                    "step_number": row.step_number,
                    "step_type": row.step_type,
                    "parent_step_id": row.parent_step_id,
                    "status": row.status,
                    "started_at": row.started_at,
                    "completed_at": row.completed_at,
                    "duration_ms": row.duration_ms,
                    "llm_input_tokens": row.llm_input_tokens,
                    "llm_output_tokens": row.llm_output_tokens,
                    "error_message": row.error_message,
                    "metadata": _json_loads(row.metadata_json, {}),
                }
                for row in rows
            ]

    async def get_by_run(self, run_id: str, include_deleted: bool = False) -> list[dict[str, Any]]:
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
            return [
                {
                    "id": row.id,
                    "session_id": row.session_id,
                    "run_id": row.run_id,
                    "step_number": row.step_number,
                    "step_type": row.step_type,
                    "parent_step_id": row.parent_step_id,
                    "status": row.status,
                    "started_at": row.started_at,
                    "completed_at": row.completed_at,
                    "duration_ms": row.duration_ms,
                    "llm_input_tokens": row.llm_input_tokens,
                    "llm_output_tokens": row.llm_output_tokens,
                    "error_message": row.error_message,
                    "metadata": _json_loads(row.metadata_json, {}),
                }
                for row in rows
            ]

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

    async def save(self, tool_call: dict[str, Any]) -> None:
        """保存工具调用记录."""
        async with get_session() as session:
            async with session.begin():
                model = ToolCallModel(
                    id=tool_call["id"],
                    session_id=tool_call["session_id"],
                    step_id=tool_call["step_id"],
                    tool_name=tool_call["tool_name"],
                    arguments_json=_json_dumps(tool_call.get("arguments", {})),
                    raw_output=tool_call.get("raw_output"),
                    status=tool_call.get("status", "pending"),
                    started_at=tool_call["started_at"],
                    completed_at=tool_call.get("completed_at"),
                    duration_ms=tool_call.get("duration_ms", 0),
                    error_message=tool_call.get("error_message"),
                    error_stack=tool_call.get("error_stack"),
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

    async def query(
        self, session_id: str, status: str | None = None, include_deleted: bool = False
    ) -> list[dict[str, Any]]:
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
            return [
                {
                    "id": row.id,
                    "session_id": row.session_id,
                    "step_id": row.step_id,
                    "tool_name": row.tool_name,
                    "arguments": _json_loads(row.arguments_json, {}),
                    "raw_output": row.raw_output,
                    "status": row.status,
                    "started_at": row.started_at,
                    "completed_at": row.completed_at,
                    "duration_ms": row.duration_ms,
                    "error_message": row.error_message,
                    "error_stack": row.error_stack,
                }
                for row in rows
            ]


# ---------------------------------------------------------------------------
# ApprovalLogRepository
# ---------------------------------------------------------------------------


class ApprovalLogRepository:
    """审批日志表 CRUD 操作."""

    async def save(self, log: dict[str, Any]) -> None:
        """保存审批日志."""
        async with get_session() as session:
            async with session.begin():
                model = ApprovalLogModel(
                    id=log["id"],
                    session_id=log["session_id"],
                    tool_call_id=log["tool_call_id"],
                    tool_name=log["tool_name"],
                    arguments_json=_json_dumps(log.get("arguments", {})),
                    risk_level=log["risk_level"],
                    decision=log["decision"],
                    decision_time_ms=log.get("decision_time_ms", 0),
                    timestamp=log["timestamp"],
                )
                session.add(model)

    async def get_by_session(
        self, session_id: str, include_deleted: bool = False
    ) -> list[dict[str, Any]]:
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
            return [
                {
                    "id": row.id,
                    "session_id": row.session_id,
                    "tool_call_id": row.tool_call_id,
                    "tool_name": row.tool_name,
                    "arguments": _json_loads(row.arguments_json, {}),
                    "risk_level": row.risk_level,
                    "decision": row.decision,
                    "decision_time_ms": row.decision_time_ms,
                    "timestamp": row.timestamp,
                }
                for row in rows
            ]

    async def query_by_tool_call(
        self, tool_call_id: str, include_deleted: bool = False
    ) -> dict[str, Any] | None:
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
            return {
                "id": row.id,
                "session_id": row.session_id,
                "tool_call_id": row.tool_call_id,
                "tool_name": row.tool_name,
                "arguments": _json_loads(row.arguments_json, {}),
                "risk_level": row.risk_level,
                "decision": row.decision,
                "decision_time_ms": row.decision_time_ms,
                "timestamp": row.timestamp,
            }
