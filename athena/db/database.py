"""SQLite 数据库管理 — 基于 SQLAlchemy ORM 的异步访问层.

本模块作为兼容层，内部委托给 Repository 实例，保持与旧代码相同的公共 API。
所有删除操作为软删除（设置 deleted_time）。
"""

from __future__ import annotations

from typing import Any

from athena.db.engine import close_engine, init_engine
from athena.db.repository import (
    ApprovalLogRepository,
    MessageRepository,
    SessionRepository,
    StepRepository,
    ToolCallRepository,
)
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

    async def create_session(self, session_id: str, title: str = "New Session") -> dict[str, Any]:
        return await self._sessions.create(session_id, title)

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        return await self._sessions.get(session_id)

    async def list_sessions(self) -> list[dict[str, Any]]:
        return await self._sessions.list_all()

    async def update_session(
        self, session_id: str, *, status: str | None = None, run_id: str | None = None,
        title: str | None = None,
    ) -> None:
        await self._sessions.update(session_id, status=status, run_id=run_id, title=title)

    async def query_sessions(self, status: list[str]) -> list[dict[str, Any]]:
        return await self._sessions.query_by_status(status)

    async def delete_session(self, session_id: str) -> None:
        """软删除会话及其所有关联数据."""
        await self._sessions.delete(session_id)

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def save_message(self, session_id: str, message: dict[str, Any]) -> str:
        msg_id = await self._messages.save(session_id, message)
        await self.update_session(session_id)  # refresh updated_at
        return msg_id

    async def get_messages(self, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        return await self._messages.get_by_session(session_id, limit=limit)

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    async def save_step(self, step: dict[str, Any]) -> None:
        await self._steps.save(step)

    async def update_step(self, step_id: str, updates: dict[str, Any]) -> None:
        await self._steps.update(step_id, updates)

    async def get_steps(self, session_id: str) -> list[dict[str, Any]]:
        return await self._steps.get_by_session(session_id)

    async def get_steps_by_run(self, run_id: str) -> list[dict[str, Any]]:
        return await self._steps.get_by_run(run_id)

    async def get_last_step_number(self, run_id: str) -> int:
        return await self._steps.get_last_step_number(run_id)

    # ------------------------------------------------------------------
    # Tool calls
    # ------------------------------------------------------------------

    async def save_tool_call(self, tool_call: dict[str, Any]) -> None:
        await self._tool_calls.save(tool_call)

    async def update_tool_call(self, tool_call_id: str, updates: dict[str, Any]) -> None:
        await self._tool_calls.update(tool_call_id, updates)

    async def query_tool_calls(
        self, session_id: str, status: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._tool_calls.query(session_id, status=status)

    # ------------------------------------------------------------------
    # Approval logs
    # ------------------------------------------------------------------

    async def save_approval_log(self, log: dict[str, Any]) -> None:
        await self._approval_logs.save(log)

    async def get_approval_logs(self, session_id: str) -> list[dict[str, Any]]:
        return await self._approval_logs.get_by_session(session_id)

    async def query_approval(self, tool_call_id: str) -> dict[str, Any] | None:
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
