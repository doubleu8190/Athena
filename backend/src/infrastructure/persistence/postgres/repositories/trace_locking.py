"""PostgreSQL Trace span 写入所需的 Run 级事务锁。"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def lock_trace_span_scope(
    session: AsyncSession,
    run_id: str,
) -> None:
    """在事务内按 Run 串行化 Trace span 序号分配。

    调用方必须在锁定同一 Run 的 Agent Run、Plan、Task、Execution 或 Command
    领域行之前调用本函数。TraceSpan 的外键插入会请求 Run 行的共享键锁；若先持有
    领域行锁再申请本 advisory lock，会与另一 Trace 事务形成相反锁序。

    参数：
    - `session`（AsyncSession）：已经参与调用方事务的数据库会话。
    - `run_id`（str）：需要串行化 Trace 序列的 Run 标识。

    返回：
    - None：锁已绑定当前事务，提交或回滚时自动释放。

    异常：
    - 数据库异常：无法申请事务锁时由 SQLAlchemy 原样传播。
    """
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended(:trace_run_id, 0))"
        ),
        {"trace_run_id": run_id},
    )


__all__ = ["lock_trace_span_scope"]
