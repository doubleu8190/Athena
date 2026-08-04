"""被动会话恢复 — 启动时检测 interrupted 会话并通知用户.

project_memory 约束：恢复采用被动模式，仅通知用户不主动执行，
避免产生意外副作用。
"""

from __future__ import annotations

from athena.db.database import Database

from athena.utils.logging import get_logger

logger = get_logger(__name__)


async def recover_interrupted_sessions(db: Database) -> None:
    """被动会话恢复：检测 interrupted 会话并标记为 idle 等待用户确认.

    Args:
        db: 数据库实例，需提供 query_sessions 和 update_session 方法
    """
    try:
        interrupted = await db.query_sessions(status=["interrupted", "running"])
        for session in interrupted:
            await db.update_session(session["id"], status="idle")
            logger.info(
                "session_recovered_passive",
                session_id=session["id"],
                previous_status=session.get("status"),
            )
    except Exception as e:
        logger.warning("recovery_check_failed", error=str(e))
