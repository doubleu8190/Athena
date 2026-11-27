"""Run 已提交事件的统一增量推送与子 Run 父频道转发辅助函数。"""

from __future__ import annotations

import logging

from app.run.events import publish_pending_events
from domain.run.ports import RunEventTransport, RunRepository

logger = logging.getLogger(__name__)


async def publish_run_events(
    repository: RunRepository,
    publisher: RunEventTransport | None,
    *,
    session_id: str,
    owner_id: str,
    run_id: str,
    execution_epoch: int,
    after_sequence: int,
    parent_run_id: str | None = None,
    target_sequence: int | None = None,
) -> tuple[int, bool]:
    """增量推送当前 Run 已提交事件；若为子 Run 且存在新事件，同步转发至父 Run 实时频道。"""
    latest_cursor, lookup_failed = await publish_pending_events(
        repository,
        publisher,
        session_id=session_id,
        owner_id=owner_id,
        run_id=run_id,
        execution_epoch=execution_epoch,
        after_sequence=after_sequence,
        target_sequence=target_sequence,
    )
    if (
        not lookup_failed
        and publisher is not None
        and parent_run_id is not None
        and latest_cursor > after_sequence
    ):
        # 子 Run 状态变更同步唤醒父 Run 实时频道，避免父 Run SSE 等待周期对账。
        events = await repository.list_events(
            session_id,
            owner_id,
            run_id,
            after_sequence=after_sequence,
            limit=100,
            through_sequence=latest_cursor,
        )
        for event in events:
            try:
                await publisher.publish_persisted(parent_run_id, execution_epoch, event)
            except Exception:
                logger.warning(
                    "subagent_parent_event_forward_failed parent_run_id=%s sub_run_id=%s",
                    parent_run_id,
                    run_id,
                )
    return latest_cursor, lookup_failed
