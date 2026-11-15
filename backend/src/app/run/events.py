"""Run 已提交事件的统一实时发布与降级对账辅助函数。"""

from __future__ import annotations

import logging

from domain.run.model import RunEvent
from domain.run.ports import RunEventTransport, RunRepository

logger = logging.getLogger(__name__)


async def publish_persisted_event(
    transport: RunEventTransport | None,
    run_id: str,
    execution_epoch: int,
    event: RunEvent,
) -> None:
    """尽力推送单条已提交事件；实时通道失败不影响已提交事实。"""
    if transport is None:
        return
    try:
        await transport.publish_persisted(run_id, execution_epoch, event)
    except Exception as error:
        # SSE 会从 PostgreSQL 对账补齐，发布异常只记录告警并忽略。
        logger.warning(
            "run_event_publish_failed run_id=%s exception_type=%s",
            run_id,
            type(error).__name__,
        )


async def publish_pending_events(
    repository: RunRepository,
    transport: RunEventTransport | None,
    *,
    session_id: str,
    owner_id: str,
    run_id: str,
    execution_epoch: int,
    after_sequence: int,
    target_sequence: int | None = None,
) -> tuple[int, bool]:
    """增量查询并推送已提交事件；返回最新游标与是否发生查询故障。"""
    if transport is None:
        return after_sequence, False
    cursor = after_sequence
    if target_sequence is not None and target_sequence <= cursor:
        return cursor, False
    try:
        while True:
            if target_sequence is None:
                current = await repository.get(session_id, owner_id, run_id)
                if current is None or current.event_sequence <= cursor:
                    return cursor, False
                ceiling = current.event_sequence
            else:
                if cursor >= target_sequence:
                    return cursor, False
                ceiling = target_sequence
            events = await repository.list_events(
                session_id,
                owner_id,
                run_id,
                after_sequence=cursor,
                limit=min(100, ceiling - cursor),
            )
            if not events:
                return cursor, False
            for event in events:
                await publish_persisted_event(transport, run_id, execution_epoch, event)
                cursor = event.sequence
    except Exception as error:
        logger.warning(
            "run_event_lookup_failed run_id=%s exception_type=%s",
            run_id,
            type(error).__name__,
        )
        return cursor, True
