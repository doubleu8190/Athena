"""同一 PostgreSQL 快照中读取已结束的会话历史。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from application.observability import ProgressEntry, ProgressSource
from application.sessions.history_port import (
    ActiveSessionSnapshot,
    HistoryRunRecord,
    SessionHistoryQueryPort,
    SessionHistorySnapshot,
    HistoryTaskRecord,
    SessionTurnSnapshot,
)
from domain.sessions import Message

from ..mappers.converters import message_model_to_domain
from ..models import (
    AgentEventModel,
    AgentRunModel,
    MessageModel,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorTaskModel,
    RunProgressModel,
    SessionModel,
    WorkerDisplayResultModel,
)


class PostgresSessionHistoryRepository(SessionHistoryQueryPort):
    """在一个只读一致性快照中批量读取轮次、消息和展示事实。"""

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
        """读取一页完整轮次和对应展示事实。

        参数：
        - `session_id`（str）：历史所属会话。
        - `before_sequence`（int | None）：只取此 Run 序号之前的数据。
        - `limit`（int）：轮次数量上限，调用方传入 1 到 100。

        返回：
        - SessionHistorySnapshot：同一快照中的 Run、消息、Progress 和任务结果。

        异常：
        - 数据库异常：读取事务失败时由 SQLAlchemy 传播。
        """
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "SET TRANSACTION ISOLATION LEVEL "
                        "REPEATABLE READ READ ONLY"
                    )
                )
                session_row = await session.get(SessionModel, session_id)
                active_run_id = (
                    session_row.active_run_id if session_row is not None else None
                )
                query = select(AgentRunModel).where(
                    AgentRunModel.session_id == session_id,
                    AgentRunModel.status.in_(("completed", "failed", "cancelled")),
                )
                if before_sequence is not None:
                    query = query.where(AgentRunModel.sequence < before_sequence)
                run_rows = list(
                    (
                        await session.execute(
                            query.order_by(
                                AgentRunModel.sequence.desc()
                            ).limit(limit + 1)
                        )
                    )
                    .scalars()
                    .all()
                )
                has_more = len(run_rows) > limit
                run_rows = run_rows[:limit]
                snapshot_event_seq = int(
                    await session.scalar(
                        select(func.max(AgentEventModel.session_seq)).where(
                            AgentEventModel.session_id == session_id
                        )
                    )
                    or 0
                )
                snapshot_progress_ingest_seq = int(
                    await session.scalar(
                        select(func.max(RunProgressModel.ingest_sequence)).where(
                            RunProgressModel.session_id == session_id
                        )
                    )
                    or 0
                )
                snapshot_run_seq = max(
                    (row.sequence for row in run_rows),
                    default=0,
                )
                if not run_rows:
                    return SessionHistorySnapshot(
                        (),
                        (),
                        (),
                        (),
                        False,
                        snapshot_event_seq,
                        snapshot_progress_ingest_seq,
                        active_run_id,
                        snapshot_run_seq,
                    )

                run_ids = [row.run_id for row in run_rows]
                messages, progress, tasks = await _load_history_child_rows(
                    session,
                    session_id=session_id,
                    run_rows=run_rows,
                )

        return SessionHistorySnapshot(
            tuple(
                HistoryRunRecord(
                    row.run_id,
                    row.message_id,
                    row.sequence,
                    row.status,
                )
                for row in run_rows
            ),
            messages,
            progress,
            tasks,
            has_more,
            snapshot_event_seq,
            snapshot_progress_ingest_seq,
            active_run_id,
            snapshot_run_seq,
        )

    async def read_stream_snapshot(
        self,
        session_id: str,
        *,
        after_event_sequence: int,
        after_progress_ingest_sequence: int,
        after_run_sequence: int | None,
        active_run_id: str | None,
        limit: int = 20,
        include_latest_if_empty: bool = False,
    ) -> ActiveSessionSnapshot:
        """读取历史水位之后一个有界页的可见 Run 事实。

        订阅在调用本方法前建立，队列会覆盖事务建立后的实时事件。本方法在
        同一只读快照中返回以下候选中按 Run 序号最早的一页：
        - 调用时仍活动的 Run；
        - 调用 `messages` 时仍活动、连接 SSE 前已经结束的 Run；
        - 在客户端事件或 Progress 水位之后出现终态事实的 Run；
        - 显式允许时，在没有候选项时返回最新 Run 以覆盖首次连接竞态。

        参数：
        - `session_id`：目标会话。
        - `after_event_sequence`：客户端历史响应已消费的持久事件水位。
        - `after_progress_ingest_sequence`：客户端历史响应已消费的 Progress 水位。
        - `after_run_sequence`：客户端已经消费的最后一轮 Run 序号；有值时，
          所有后续终态 Run 都按轮次补齐，不依赖 event 或 Progress 是否已落库。
        - `active_run_id`：`messages` 或 SSE 令牌中的活动 Run 边界；只有该 Run
          允许使用 `sequence >= after_run_sequence` 恢复。
        - `limit`：单页最多包含的 Run 数；每个 Run 内的 Worker 组不可拆分。
        - `include_latest_if_empty`：仅首次无游标连接时为 True，返回最新 Run
          覆盖建连竞态；后续分页必须为 False，避免重复返回。

        返回：
        - `ActiveSessionSnapshot`：包含完整轮次、分页状态和同一事务内的水位。

        异常：
        - 数据库异常：查询失败时由 SQLAlchemy 传播。
        """
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                )
                session_row = await session.get(SessionModel, session_id)
                snapshot_event_seq = int(
                    await session.scalar(
                        select(func.max(AgentEventModel.session_seq)).where(
                            AgentEventModel.session_id == session_id
                        )
                    )
                    or 0
                )
                snapshot_progress_ingest_seq = int(
                    await session.scalar(
                        select(func.max(RunProgressModel.ingest_sequence)).where(
                            RunProgressModel.session_id == session_id
                        )
                    )
                    or 0
                )

                active_candidate_ids: list[str] = []
                if session_row is not None and session_row.active_run_id:
                    active_candidate_ids.append(session_row.active_run_id)
                if active_run_id:
                    active_candidate_ids.append(active_run_id)

                terminal_conditions = [
                    AgentRunModel.session_id == session_id,
                    AgentRunModel.status.in_(
                        ("completed", "failed", "cancelled")
                    ),
                ]
                if after_run_sequence is not None:
                    # Run 序号是历史轮次的权威边界。不能要求 outbox event
                    # 或 Progress 已经可见，否则快速连续完成时可能漏掉中间轮次。
                    terminal_conditions.append(
                        AgentRunModel.sequence > after_run_sequence
                    )
                else:
                    # 无 messages 令牌的手动连接仍以 event/Progress 水位为基线。
                    has_later_event = select(AgentEventModel.session_seq).where(
                        AgentEventModel.session_id == session_id,
                        AgentEventModel.run_id == AgentRunModel.run_id,
                        AgentEventModel.session_seq > after_event_sequence,
                    ).exists()
                    has_later_progress = select(
                        RunProgressModel.ingest_sequence
                    ).where(
                        RunProgressModel.session_id == session_id,
                        RunProgressModel.run_id == AgentRunModel.run_id,
                        RunProgressModel.ingest_sequence
                        > after_progress_ingest_sequence,
                    ).exists()
                    terminal_conditions.append(
                        has_later_event | has_later_progress
                    )
                terminal_condition = and_(*terminal_conditions)
                candidate_condition = terminal_condition
                unique_active_ids = tuple(dict.fromkeys(active_candidate_ids))
                if unique_active_ids:
                    active_condition = and_(
                        AgentRunModel.session_id == session_id,
                        AgentRunModel.run_id.in_(unique_active_ids),
                    )
                    if after_run_sequence is not None:
                        active_condition = and_(
                            active_condition,
                            or_(
                                AgentRunModel.sequence > after_run_sequence,
                                AgentRunModel.run_id == active_run_id,
                            ),
                        )
                    candidate_condition = or_(
                        terminal_condition,
                        active_condition,
                    )
                page_limit = max(1, min(limit, 100))
                page_rows = list(
                    (
                        await session.execute(
                            select(AgentRunModel)
                            .where(candidate_condition)
                            .order_by(AgentRunModel.sequence)
                            .limit(page_limit + 1)
                        )
                    )
                    .scalars()
                    .all()
                )
                has_more = len(page_rows) > page_limit
                run_rows = page_rows[:page_limit]
                if not run_rows and include_latest_if_empty:
                    latest = await session.scalar(
                        select(AgentRunModel)
                        .where(AgentRunModel.session_id == session_id)
                        .order_by(AgentRunModel.sequence.desc())
                        .limit(1)
                    )
                    if latest is not None:
                        run_rows = [latest]

                messages, progress, tasks = await _load_history_child_rows(
                    session,
                    session_id=session_id,
                    run_rows=run_rows,
                )
                turns = _turn_snapshots(run_rows, messages, progress, tasks)
                return ActiveSessionSnapshot(
                    turns,
                    snapshot_event_seq,
                    snapshot_progress_ingest_seq,
                    max(
                        (row.sequence for row in run_rows),
                        default=after_run_sequence or 0,
                    ),
                    has_more,
                )


async def _load_history_child_rows(
    session: AsyncSession,
    *,
    session_id: str,
    run_rows: list[AgentRunModel],
) -> tuple[
    tuple[Message, ...],
    tuple[ProgressEntry, ...],
    tuple[HistoryTaskRecord, ...],
]:
    """在同一数据库快照中读取一个或多个轮次的全部展示事实。"""
    run_ids = [row.run_id for row in run_rows]
    message_ids = [row.message_id for row in run_rows]
    message_rows = list(
        (
            await session.execute(
                select(MessageModel)
                .where(
                    MessageModel.session_id == session_id,
                    MessageModel.deleted_time.is_(None),
                    MessageModel.id.in_(message_ids)
                    | (
                        (MessageModel.run_id.in_(run_ids))
                        & (MessageModel.role == "assistant")
                    ),
                )
                .order_by(MessageModel.timestamp, MessageModel.id)
            )
        )
        .scalars()
        .all()
    )
    progress_rows = (
        await session.execute(
            select(RunProgressModel)
            .where(RunProgressModel.run_id.in_(run_ids))
            .order_by(RunProgressModel.ingest_sequence)
        )
    ).scalars()
    execution_rows = (
        await session.execute(
            select(
                OrchestratorExecutionModel,
                OrchestratorTaskModel,
                OrchestratorPlanModel.run_id,
                WorkerDisplayResultModel,
            )
            .join(
                OrchestratorTaskModel,
                OrchestratorTaskModel.task_id == OrchestratorExecutionModel.task_id,
            )
            .join(
                OrchestratorPlanModel,
                OrchestratorPlanModel.plan_id == OrchestratorTaskModel.plan_id,
            )
            .outerjoin(
                WorkerDisplayResultModel,
                WorkerDisplayResultModel.execution_id
                == OrchestratorExecutionModel.execution_id,
            )
            .where(OrchestratorPlanModel.run_id.in_(run_ids))
            .order_by(
                OrchestratorExecutionModel.created_at,
                OrchestratorExecutionModel.execution_id,
            )
        )
    ).all()
    tasks = tuple(
        HistoryTaskRecord(
            run_id=run_id,
            task_id=task.task_id,
            execution_id=execution.execution_id,
            worker_name=execution.worker_name or execution.instance_id or "Worker",
            task_title=task.objective,
            status=execution.status,
            created_at=execution.created_at,
            completed_at=execution.completed_at,
            display_result=(
                display_result.display_result
                if display_result is not None
                else None
            ),
            display_result_at=(
                display_result.display_result_at
                if display_result is not None
                else None
            ),
        )
        for execution, task, run_id, display_result in execution_rows
    )
    progress = tuple(
        ProgressEntry(
            progress_id=row.progress_id,
            session_id=row.session_id,
            run_id=row.run_id,
            execution_id=row.execution_id,
            task_id=row.task_id,
            worker_id=row.worker_id,
            source=ProgressSource(row.source),
            sequence=row.sequence,
            content=row.content,
            created_at=row.created_at,
            ingest_sequence=row.ingest_sequence,
        )
        for row in progress_rows
    )
    return (
        tuple(message_model_to_domain(row) for row in message_rows),
        progress,
        tasks,
    )


def _turn_snapshots(
    run_rows: list[AgentRunModel],
    messages: tuple[Message, ...],
    progress: tuple[ProgressEntry, ...],
    tasks: tuple[HistoryTaskRecord, ...],
) -> tuple[SessionTurnSnapshot, ...]:
    """按 Run 归组已加载事实，避免一个用户消息的执行过程跨快照拆分。

    参数：
    - `run_rows`：需要恢复的 Run，顺序已按会话序号排列。
    - `messages`：这些 Run 的用户消息和最终 assistant 事实。
    - `progress`：这些 Run 的 Root、Orchestrator 和 Worker Progress。
    - `tasks`：这些 Run 下每次 Worker execution 的展示事实。

    返回：
    - `tuple[SessionTurnSnapshot, ...]`：每个 Run 一个完整展示分组。

    异常：
    - 无：纯内存归组不主动抛出业务异常。
    """
    return tuple(
        SessionTurnSnapshot(
            run=HistoryRunRecord(
                row.run_id,
                row.message_id,
                row.sequence,
                row.status,
            ),
            messages=tuple(
                item
                for item in messages
                if item.id == row.message_id
                or (
                    item.role.value == "assistant"
                    and item.run_id == row.run_id
                )
            ),
            progress=tuple(
                item for item in progress if item.run_id == row.run_id
            ),
            tasks=tuple(item for item in tasks if item.run_id == row.run_id),
        )
        for row in run_rows
    )
