"""工具调用记录 SQLite 仓库。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update

from athena.infrastructure.sqlite.engine import get_session
from athena.infrastructure.sqlite.models import ToolCallModel
from athena.models import ToolCallRecord

from .converters import _row_to_tool_call
from .repository_utils import _json_dumps


class ToolCallRepository:
    """工具调用记录表 CRUD 操作。"""

    async def save(self, tool_call: ToolCallRecord) -> None:
        """保存工具调用记录。"""
        async with get_session() as session:
            async with session.begin():
                session.add(
                    ToolCallModel(
                        id=tool_call.id,
                        session_id=tool_call.session_id,
                        tool_name=tool_call.tool_name,
                        arguments_json=_json_dumps(tool_call.arguments),
                        raw_output=tool_call.raw_output,
                        status=tool_call.status.value,
                        started_at=tool_call.started_at.isoformat(),
                        completed_at=(
                            tool_call.completed_at.isoformat()
                            if tool_call.completed_at
                            else None
                        ),
                        duration_ms=tool_call.duration_ms,
                        error_message=tool_call.error_message,
                        error_stack=tool_call.error_stack,
                    )
                )

    async def get(self, tool_call_id: str) -> ToolCallRecord | None:
        """按稳定账本 ID 查询一次工具调用。"""

        async with get_session() as session:
            row = await session.get(ToolCallModel, tool_call_id)
            if row is None or row.deleted_time is not None:
                return None
            return _row_to_tool_call(row)

    @staticmethod
    def _allowed_values(updates: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "raw_output",
            "status",
            "completed_at",
            "duration_ms",
            "error_message",
            "error_stack",
        }
        return {key: value for key, value in updates.items() if key in allowed}

    async def update(self, tool_call_id: str, updates: dict[str, Any]) -> None:
        """更新工具调用记录。"""
        values = self._allowed_values(updates)
        if not values:
            return
        async with get_session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolCallModel)
                    .where(
                        ToolCallModel.id == tool_call_id,
                        ToolCallModel.deleted_time.is_(None),
                    )
                    .values(**values)
                )

    async def query(
        self, session_id: str, status: str | None = None, include_deleted: bool = False
    ) -> list[ToolCallRecord]:
        """查询工具调用记录。"""
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
            return [_row_to_tool_call(row) for row in result.scalars().all()]

    async def last_called_by_tool(self) -> dict[str, str]:
        """返回工具名到最近调用时间的映射。"""
        async with get_session() as session:
            stmt = (
                select(ToolCallModel.tool_name, func.max(ToolCallModel.started_at))
                .where(ToolCallModel.deleted_time.is_(None))
                .group_by(ToolCallModel.tool_name)
            )
            result = await session.execute(stmt)
            return {name: last for name, last in result.all()}

    async def count_calls_since(self, since: datetime) -> int:
        """统计指定时间点之后的工具调用次数。"""
        async with get_session() as session:
            stmt = select(func.count(ToolCallModel.id)).where(
                ToolCallModel.started_at >= since.isoformat(),
                ToolCallModel.deleted_time.is_(None),
            )
            result = await session.execute(stmt)
            return int(result.scalar_one())
