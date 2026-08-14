"""SQLite 数据库管理 — 基于 SQLAlchemy ORM 的异步访问层.

Database 持有各 Repository 实例并提供 connect/close 生命周期管理。
调用者通过公开属性直接访问 Repository（如 db.messages.save(msg)）。
所有删除操作为软删除（设置 deleted_time）。
"""

from __future__ import annotations

from datetime import datetime

from athena.db.engine import close_engine, init_engine
from athena.db.repository import (
    ApprovalLogRepository,
    McpServerRepository,
    MessageRepository,
    SessionRepository,
    StepRepository,
    ToolCallRepository,
)
from athena.models.step import StepStatus
from athena.models.tool import ToolCallStatus
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class Database:
    """异步数据库访问层.

    公开属性暴露各 Repository，调用者直接使用（如 db.messages.save(msg)）。
    connect/close 管理 SQLAlchemy 引擎生命周期。
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self.sessions = SessionRepository()
        self.messages = MessageRepository()
        self.steps = StepRepository()
        self.tool_calls = ToolCallRepository()
        self.approval_logs = ApprovalLogRepository()
        self.mcp_servers = McpServerRepository()

    async def connect(self) -> None:
        """建立连接并初始化表结构."""
        await init_engine(self._db_path)
        logger.info("database_connected", db_path=self._db_path)

    async def close(self) -> None:
        """关闭数据库连接."""
        await close_engine()
        logger.info("database_closed")

    async def cleanup_interrupted_session(self, session_id: str) -> None:
        """清理进程中断遗留的 running 步骤/工具调用，统一标记为 failed.

        仅在服务启动恢复阶段调用（此时不存在运行中的 run）。
        状态值必须用 enum 合法值（failed），否则 repository 反序列化时
        _row_to_step / _row_to_tool_call 的枚举转换会抛 ValueError。
        """
        now = datetime.now().isoformat()
        err = "进程中断(服务重启)"
        await self.steps.update_running_by_session(
            session_id,
            {
                "status": str(StepStatus.FAILED),
                "completed_at": now,
                "error_message": err,
            },
        )
        await self.tool_calls.update_running_by_session(
            session_id,
            {
                "status": str(ToolCallStatus.FAILED),
                "completed_at": now,
                "error_message": err,
            },
        )


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
