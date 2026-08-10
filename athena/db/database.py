"""SQLite 数据库管理 — 基于 SQLAlchemy ORM 的异步访问层.

本模块作为兼容层，内部委托给 Repository 实例，保持与旧代码相同的公共 API。
查询返回类型化领域模型（athena.models），save 接受类型化模型。
所有删除操作为软删除（设置 deleted_time）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from athena.db.engine import close_engine, init_engine
from athena.db.repository import (
    ApprovalLogRepository,
    MessageRepository,
    SessionRepository,
    StepRepository,
    ToolCallRepository,
    _SENTINEL,
)
from athena.models import (
    ApprovalLog,
    Message,
    Session,
    Step,
    ToolCallRecord,
)
from athena.models.step import StepStatus
from athena.models.tool import ToolCallStatus
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class Database:
    """异步数据库访问层（兼容层）.

    内部委托给 Repository 实例，保持与旧代码相同的 API。
    所有删除操作为软删除（设置 deleted_time）。
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._sessions = SessionRepository()
        self._messages = MessageRepository()
        self._steps = StepRepository()
        self._tool_calls = ToolCallRepository()
        self._approval_logs = ApprovalLogRepository()

    async def connect(self) -> None:
        """建立连接并初始化表结构."""
        await init_engine(self._db_path)
        logger.info("database_connected", db_path=self._db_path)

    async def close(self) -> None:
        """关闭数据库连接."""
        await close_engine()
        logger.info("database_closed")

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def create_session(self, session_id: str, title: str = "New Session") -> Session:
        return await self._sessions.create(session_id, title)

    async def get_session(self, session_id: str) -> Session | None:
        return await self._sessions.get(session_id)

    async def list_sessions(self) -> list[Session]:
        return await self._sessions.list_all()

    async def update_session(
        self, session_id: str, *, status: str | None = None, run_id: str | None = None,
        title: str | None = None,
        compression_summary: str | None = _SENTINEL,
        last_compressed_message_id: str | None = _SENTINEL,
        last_summarized_message_id: str | None = _SENTINEL,
    ) -> None:
        await self._sessions.update(
            session_id, status=status, run_id=run_id, title=title,
            compression_summary=compression_summary,
            last_compressed_message_id=last_compressed_message_id,
            last_summarized_message_id=last_summarized_message_id,
        )

    async def query_sessions(self, status: list[str]) -> list[Session]:
        return await self._sessions.query_by_status(status)

    async def delete_session(self, session_id: str) -> None:
        """软删除会话及其所有关联数据."""
        await self._sessions.delete(session_id)

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def save_message(self, message: Message) -> str:
        # MessageRepository.save 已在同一事务内刷新 session.updated_at，无需额外调用
        return await self._messages.save(message)

    async def get_messages(self, session_id: str, limit: int | None = None) -> list[Message]:
        return await self._messages.get_by_session(session_id, limit=limit)

    async def get_messages_after(self, session_id: str, after_id: str) -> list[Message]:
        """获取指定消息之后的消息列表（用于增量压缩）."""
        return await self._messages.get_after_message(session_id, after_id)

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    async def save_step(self, step: Step) -> None:
        await self._steps.save(step)

    async def update_step(self, step_id: str, updates: dict[str, Any]) -> None:
        await self._steps.update(step_id, updates)

    async def get_steps(self, session_id: str) -> list[Step]:
        return await self._steps.get_by_session(session_id)

    async def get_steps_by_run(self, run_id: str) -> list[Step]:
        return await self._steps.get_by_run(run_id)

    async def get_last_step_number(self, run_id: str) -> int:
        return await self._steps.get_last_step_number(run_id)

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------

    async def save_tool_call(self, tool_call: ToolCallRecord) -> None:
        await self._tool_calls.save(tool_call)

    async def update_tool_call(self, tool_call_id: str, updates: dict[str, Any]) -> None:
        await self._tool_calls.update(tool_call_id, updates)

    async def query_tool_calls(
        self, session_id: str, status: str | None = None
    ) -> list[ToolCallRecord]:
        return await self._tool_calls.query(session_id, status=status)

    # ------------------------------------------------------------------
    # 会话中断清理
    # ------------------------------------------------------------------

    async def cleanup_interrupted_session(self, session_id: str) -> None:
        """清理进程中断遗留的 running 步骤/工具调用，统一标记为 failed.

        仅在服务启动恢复阶段调用（此时不存在运行中的 run）。
        状态值必须用 enum 合法值（failed），否则 repository 反序列化时
        _row_to_step / _row_to_tool_call 的枚举转换会抛 ValueError。
        """
        now = datetime.now().isoformat()
        err = "进程中断(服务重启)"
        await self._steps.update_running_by_session(session_id, {
            "status": str(StepStatus.FAILED),
            "completed_at": now,
            "error_message": err,
        })
        await self._tool_calls.update_running_by_session(session_id, {
            "status": str(ToolCallStatus.FAILED),
            "completed_at": now,
            "error_message": err,
        })

    # ------------------------------------------------------------------
    # Approval logs
    # ------------------------------------------------------------------

    async def save_approval_log(self, log: ApprovalLog) -> None:
        await self._approval_logs.save(log)

    async def get_approval_logs(self, session_id: str) -> list[ApprovalLog]:
        return await self._approval_logs.get_by_session(session_id)

    async def query_approval(self, tool_call_id: str) -> ApprovalLog | None:
        return await self._approval_logs.query_by_tool_call(tool_call_id)


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_db_instance: Database | None = None


async def get_database(db_path: str) -> Database:
    """获取数据库单例."""
    global _db_instance
    if _db_instance is None:
        _db_instance = Database(db_path)
        await _db_instance.connect()
    return _db_instance


async def close_database() -> None:
    global _db_instance
    if _db_instance is not None:
        await _db_instance.close()
        _db_instance = None
