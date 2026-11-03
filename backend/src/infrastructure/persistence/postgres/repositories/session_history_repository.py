"""同一 PostgreSQL 快照中读取已结束的会话历史。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from application.sessions.history_port import (
    HistoryRunRecord,
    SessionHistoryQueryPort,
    SessionHistorySnapshot,
)
from domain.events import ApplicationEvent
from domain.sessions import Message

from ..mappers.converters import message_model_to_domain
from ..models import AgentEventModel, AgentRunModel, MessageModel
from .event_repository import _event_from_model

_OUTPUT_EVENTS = (
    "message.started", "message.delta", "message.completed", "stream.snapshot",
    "thinking.started", "thinking.summary", "thinking.completed",
)
_ACTIVE_RUN_STATUSES = ("queued", "running", "stopping")


def _continuous_visible_prefix_watermark(
    candidate_max: int,
    first_unreturned_visible_event_seq: int | None,
) -> int:
    """计算本页可见事件连续已消费前缀。

    参数：
    - `candidate_max`（int）：本页实际返回事件中的最大会话序号。
    - `first_unreturned_visible_event_seq`（int | None）：候选范围内第一个可被 SSE 投递、
      但未被本页返回的事件序号；停止后应隐藏且不会被 SSE 投递的事件不会出现在该值中。

    返回：
    - int：可安全越过所有连续已返回可见事件的最大会话序号。

    异常：
    - 无：该纯计算函数不主动抛出业务异常。
    """
    if first_unreturned_visible_event_seq is None:
        return max(0, candidate_max)
    return max(
        0,
        min(candidate_max, first_unreturned_visible_event_seq - 1),
    )


def _history_snapshot_event_seq(
    consumed_visible_prefix_watermark: int,
    earliest_active_event_seq: int | None,
) -> int:
    """计算历史响应与 SSE 之间安全的事件水位。

    参数：
    - `consumed_visible_prefix_watermark`（int）：本页连续已消费的可见事件水位。
    - `earliest_active_event_seq`（int | None）：同一快照内未终态 Run 的最早事件序号。

    返回：
    - int：不会越过任一未消费 active 事件、且不小于零的 SSE 恢复游标。

    异常：
    - 无：该纯计算函数不主动抛出业务异常。
    """
    if earliest_active_event_seq is None:
        return max(0, consumed_visible_prefix_watermark)
    return max(
        0,
        min(
            consumed_visible_prefix_watermark,
            earliest_active_event_seq - 1,
        ),
    )


def _hidden_stopped_output_condition() -> ColumnElement[bool]:
    """返回停止后不应由历史列表或 SSE 展示的输出事件条件。

    参数：
    - 无。

    返回：
    - ColumnElement[bool]：与当前事件行相关联的 EXISTS 条件。

    异常：
    - 无：仅构造 SQLAlchemy 表达式。
    """
    return select(AgentRunModel.run_id).where(
        AgentRunModel.run_id == AgentEventModel.run_id,
        AgentRunModel.display_cutoff_session_seq.is_not(None),
        AgentEventModel.session_seq > AgentRunModel.display_cutoff_session_seq,
        AgentEventModel.event_type.in_(_OUTPUT_EVENTS),
    ).exists()


class PostgresSessionHistoryRepository(SessionHistoryQueryPort):
    """在一个只读一致性快照中批量读取轮次、消息和持久事件。"""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def read_snapshot(
        self,
        session_id: str,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> SessionHistorySnapshot:
        """读取一页完整轮次和对应事件。

        参数：
        - `session_id`（str）：历史所属会话。
        - `before_sequence`（int | None）：只取此 Run 序号之前的数据。
        - `limit`（int）：轮次数量上限，调用方传入 1 到 100。

        返回：
        - SessionHistorySnapshot：同一 REPEATABLE READ 快照中的 Run、消息和事件。

        异常：
        - 数据库异常：读取事务失败时由 SQLAlchemy 传播。
        """
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                query = select(AgentRunModel).where(
                    AgentRunModel.session_id == session_id,
                    AgentRunModel.status.in_(("completed", "failed", "cancelled")),
                )
                if before_sequence is not None:
                    query = query.where(AgentRunModel.sequence < before_sequence)
                run_rows = list((await session.execute(
                    query.order_by(AgentRunModel.sequence.desc()).limit(limit + 1)
                )).scalars().all())
                has_more = len(run_rows) > limit
                run_rows = run_rows[:limit]
                earliest_active_event_seq = await session.scalar(
                    select(func.min(AgentEventModel.session_seq))
                    .select_from(AgentRunModel)
                    .join(
                        AgentEventModel,
                        AgentEventModel.run_id == AgentRunModel.run_id,
                    )
                    .where(
                        AgentRunModel.session_id == session_id,
                        AgentEventModel.session_id == session_id,
                        AgentRunModel.status.in_(_ACTIVE_RUN_STATUSES),
                    )
                )
                earliest_active_seq = (
                    int(earliest_active_event_seq)
                    if earliest_active_event_seq is not None
                    else None
                )
                if not run_rows:
                    snapshot_event_seq = _history_snapshot_event_seq(
                        0,
                        earliest_active_seq,
                    )
                    return SessionHistorySnapshot(
                        (),
                        (),
                        (),
                        False,
                        snapshot_event_seq,
                    )

                run_ids = [row.run_id for row in run_rows]
                message_ids = [row.message_id for row in run_rows]
                message_rows = list((await session.execute(
                    select(MessageModel).where(
                        MessageModel.session_id == session_id,
                        MessageModel.deleted_time.is_(None),
                        (MessageModel.id.in_(message_ids))
                        | ((MessageModel.run_id.in_(run_ids)) & (MessageModel.role == "assistant")),
                    ).order_by(MessageModel.timestamp, MessageModel.id)
                )).scalars().all())
                hidden_stopped_output = _hidden_stopped_output_condition()
                event_rows = list((await session.execute(
                    select(AgentEventModel).where(
                        AgentEventModel.session_id == session_id,
                        AgentEventModel.run_id.in_(run_ids),
                        ~hidden_stopped_output,
                    ).order_by(AgentEventModel.session_seq)
                )).scalars().all())
                candidate_max = max(
                    (int(event.session_seq) for event in event_rows),
                    default=0,
                )
                returned_event_sequences = {
                    int(event.session_seq)
                    for event in event_rows
                }
                first_unreturned_visible_event_seq = (
                    await session.scalar(
                        select(func.min(AgentEventModel.session_seq)).where(
                            AgentEventModel.session_id == session_id,
                            AgentEventModel.session_seq <= candidate_max,
                            AgentEventModel.session_seq.notin_(
                                returned_event_sequences
                            ),
                            ~_hidden_stopped_output_condition(),
                        )
                    )
                    if candidate_max > 0
                    else None
                )
                consumed_visible_prefix_watermark = (
                    _continuous_visible_prefix_watermark(
                        candidate_max,
                        (
                            int(first_unreturned_visible_event_seq)
                            if first_unreturned_visible_event_seq is not None
                            else None
                        ),
                    )
                )
                snapshot_event_seq = _history_snapshot_event_seq(
                    consumed_visible_prefix_watermark,
                    earliest_active_seq,
                )

        runs = tuple(HistoryRunRecord(row.run_id, row.message_id, row.sequence) for row in run_rows)
        messages: list[Message] = [message_model_to_domain(row) for row in message_rows]
        events: list[ApplicationEvent] = [_event_from_model(row) for row in event_rows]
        return SessionHistorySnapshot(
            runs,
            tuple(messages),
            tuple(events),
            has_more,
            snapshot_event_seq,
        )
