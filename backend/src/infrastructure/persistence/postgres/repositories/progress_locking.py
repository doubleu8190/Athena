"""Progress 写入所需的 PostgreSQL 事务锁。"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def lock_progress_session(
    session: AsyncSession,
    session_id: str,
) -> None:
    """串行化同一会话内 Progress 的分配与提交顺序。

    `ingest_sequence` 由全局 Identity 生成，但提交顺序并不保证与分配顺序一致。
    SSE 以会话级水位恢复数据，因此同一引用会话的写入必须在事务提交前持有同一把
    advisory lock，避免较小序号在较大序号之后提交而被恢复游标跳过。

    参数：
    - `session`（AsyncSession）：已经参与调用方事务的数据库会话。
    - `session_id`（str）：Progress 所属引用会话。

    返回：
    - None：锁已绑定到当前事务；事务提交或回滚时由 PostgreSQL 自动释放。

    异常：
    - 数据库异常：无法申请事务锁时由 SQLAlchemy 原样传播。
    """
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended(:progress_session_id, 0))"
        ),
        {"progress_session_id": session_id},
    )


__all__ = ["lock_progress_session"]
