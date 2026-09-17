"""审批日志 SQLite 仓库。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from athena.infrastructure.sqlite.engine import get_core_session
from athena.infrastructure.sqlite.models import ApprovalLogModel
from athena.models import ApprovalLog

from .model_converters import _row_to_approval_log
from .repository_utils import _json_dumps


class ApprovalLogRepository:
    """审批日志表 CRUD 操作。"""

    async def save(self, log: ApprovalLog) -> None:
        """保存审批日志。"""
        async with get_core_session() as session:
            async with session.begin():
                session.add(
                    ApprovalLogModel(
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
                )

    async def get_by_session(
        self, session_id: str, include_deleted: bool = False
    ) -> list[ApprovalLog]:
        """获取会话的审批日志。"""
        async with get_core_session() as session:
            stmt = (
                select(ApprovalLogModel)
                .where(ApprovalLogModel.session_id == session_id)
                .order_by(ApprovalLogModel.timestamp.asc())
            )
            if not include_deleted:
                stmt = stmt.where(ApprovalLogModel.deleted_time.is_(None))
            result = await session.execute(stmt)
            return [_row_to_approval_log(row) for row in result.scalars().all()]

    async def list_all(
        self, limit: int = 50, offset: int = 0, session_id: str | None = None
    ) -> list[ApprovalLog]:
        """分页列出审批日志，按时间倒序，可选按会话过滤。"""
        async with get_core_session() as session:
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
            return [_row_to_approval_log(row) for row in result.scalars().all()]

    async def stats(self) -> dict[str, int]:
        """统计今日审批决策数。"""
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        async with get_core_session() as session:
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
            return {
                "today_total": sum(counts.values()),
                "today_approved": counts.get("approved", 0),
                "today_denied": counts.get("denied", 0),
                "today_timeout": counts.get("timeout", 0),
            }
