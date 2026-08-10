"""被动会话恢复 — 启动时检测 interrupted 会话并通知用户.

project_memory 约束：恢复采用被动模式，仅通知用户不主动执行，
避免产生意外副作用。
"""

from __future__ import annotations

from athena.db.database import Database

from athena.utils.logging import get_logger

logger = get_logger(__name__)


async def recover_interrupted_sessions(db: Database) -> None:
    """被动会话恢复：检测 interrupted/running 会话并标记为 idle，清理遗留 running 步骤.

    不变式：仅应在应用 lifespan 启动阶段调用（此时尚未接受连接、不存在
    运行中的 run），故 status ∈ {running, interrupted} 的会话必然是上次
    进程被杀/中断留下的僵尸。恢复仅置位，不主动执行，避免产生副作用。

    Args:
        db: 数据库实例，需提供 query_sessions 和 update_session 方法
    """
    try:
        interrupted = await db.sessions.query_by_status(["interrupted", "running"])
        for session in interrupted:
            await db.sessions.update(session.id, status="idle")
            # 清理进程中断遗留的 running steps/tool_calls（否则永久卡 running）
            await db.cleanup_interrupted_session(session.id)
            logger.info(
                "session_recovered_passive",
                session_id=session.id,
                previous_status=session.status.value,
            )
    except Exception as e:
        logger.warning("recovery_check_failed", error=str(e))
