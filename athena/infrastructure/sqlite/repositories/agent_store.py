"""版本化命令、事件和流快照的 SQLite 持久化实现。"""

from __future__ import annotations

import json
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from athena.runtime.command_notifications import CommandNotifier
from athena.runtime.transport import SessionEventBus
from athena.contracts.commands import Command, CommandType
from athena.contracts.errors import ErrorDetail
from athena.contracts.events import ApplicationEvent, EventDurability, EventType
from athena.contracts.ports import AgentCommandRecord, AgentRunRecord
from athena.contracts.statuses import (
    AgentApprovalDecision,
    AgentApprovalStatus,
    AgentCommandStatus,
    AgentRunStatus,
    StreamSnapshotStatus,
)
from athena.infrastructure.sqlite.engine import (
    SQLITE_BUSY_TIMEOUT_MS,
    get_core_session,
)
from athena.infrastructure.sqlite.models import (
    AgentCommandModel,
    AgentEventModel,
    StreamSnapshotModel,
    AgentRunModel,
    ApprovalRecordModel,
    SessionModel,
    StepModel,
    ToolCallModel,
)
from .repository_utils import _json_dumps
from athena.models.tool import RiskLevel
from athena.utils.id_generation import generate_time_id


def _now() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。

    返回值:
        str: 带时区信息的 UTC 时间。
    异常:
        不抛出业务异常。
    """
    return datetime.now(timezone.utc).isoformat()


class AgentStore:
    """保存 Runtime 的命令、运行、事件、快照、审批和工具执行记录。"""

    def __init__(
        self,
        transport: SessionEventBus | None = None,
        command_notifier: CommandNotifier | None = None,
    ) -> None:
        """创建 Agent Store。

        参数:
            transport (SessionEventBus | None): 可选会话事件总线；为空时只持久化事件。
            command_notifier (CommandNotifier | None): Command 持久化后的进程内唤醒通知器。
        返回值:
            None。
        异常:
            不抛出业务异常。
        """
        self.transport = transport
        self.command_notifier = command_notifier
        self._event_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._session_seq_cache: dict[str, int] = {}
        self._stream_chunk_cache: dict[tuple[str, str], int] = {}

    transport: SessionEventBus | None
    command_notifier: CommandNotifier | None
    _event_locks: dict[str, asyncio.Lock]
    _session_seq_cache: dict[str, int]
    _stream_chunk_cache: dict[tuple[str, str], int]

    async def _cached_session_seq(self, db: AsyncSession, session_id: str) -> int:
        """读取或初始化会话事件游标缓存。

        参数:
            db (AsyncSession): 当前已开启事务的数据库会话。
            session_id (str): 会话 ID。
        返回值:
            int: 当前会话已持久化的最大 session_seq；没有事件时返回 0。
        异常:
            数据库查询失败时传播 SQLAlchemy 异常。
        """
        if session_id not in self._session_seq_cache:
            self._session_seq_cache[session_id] = (
                await db.scalar(
                    select(func.max(AgentEventModel.session_seq)).where(
                        AgentEventModel.session_id == session_id
                    )
                )
                or 0
            )
        return self._session_seq_cache[session_id]

    async def _cached_stream_chunk_id(
        self, db: AsyncSession, session_id: str, stream_id: str
    ) -> int:
        """读取或初始化指定流的 Chunk 序号缓存。

        参数:
            db (AsyncSession): 当前已开启事务的数据库会话。
            session_id (str): 流所属会话 ID。
            stream_id (str): 流标识。
        返回值:
            int: 当前流已持久化的最大 chunk_id；没有 Chunk 时返回 0。
        异常:
            数据库查询失败时传播 SQLAlchemy 异常。
        """
        cache_key = (session_id, stream_id)
        if cache_key not in self._stream_chunk_cache:
            self._stream_chunk_cache[cache_key] = (
                await db.scalar(
                    select(func.max(AgentEventModel.chunk_id)).where(
                        AgentEventModel.session_id == session_id,
                        AgentEventModel.stream_id == stream_id,
                    )
                )
                or 0
            )
        return self._stream_chunk_cache[cache_key]

    def _advance_stream_chunk_cache(
        self, cache_key: tuple[str, str], chunk_id: int
    ) -> None:
        """提交成功后推进流 Chunk 游标缓存，且不允许缓存回退。

        参数:
            cache_key (tuple[str, str]): 会话 ID 和流 ID 组成的缓存键。
            chunk_id (int): 已成功持久化的 Chunk 序号。
        返回值:
            None: 缓存不存在时不创建缓存，避免为显式 Chunk 额外查询数据库。
        异常:
            不抛出业务异常。
        """
        current = self._stream_chunk_cache.get(cache_key)
        if current is not None:
            # 缓存记录的是已知最大值；乱序 Chunk 或重试不能把它覆盖成较小值。
            self._stream_chunk_cache[cache_key] = max(current, chunk_id)

    async def get_active_run(self, session_id: str) -> AgentRunModel | None:
        """查询会话最近的活跃运行，包括暂停中的运行。

        参数:
            session_id (str): 非空会话 ID。
        返回值:
            AgentRunModel | None: 活跃运行记录；不存在时返回 ``None``。
        异常:
            数据库查询失败时传播 SQLAlchemy 异常。
        """
        async with get_core_session() as db:
            return await db.scalar(
                select(AgentRunModel)
                .where(
                    AgentRunModel.session_id == session_id,
                    AgentRunModel.parent_run_id.is_(None),
                    AgentRunModel.status.in_(
                        status.value
                        for status in (
                            AgentRunStatus.QUEUED,
                            AgentRunStatus.RUNNING,
                            AgentRunStatus.PAUSED,
                            AgentRunStatus.WAITING_APPROVAL,
                            AgentRunStatus.CANCEL_REQUESTED,
                        )
                    ),
                )
                .order_by(AgentRunModel.updated_at.desc())
                .limit(1)
            )

    async def prepare_runs_for_manual_recovery(self) -> dict[str, int]:
        """暂停异常退出留下的运行，并把消息命令保持在队列外。

        参数:
            无。
        返回值:
            dict[str, int]: 被暂停的运行数和被挂起的消息命令数。
        异常:
            数据库写入失败时传播 SQLAlchemy 异常。

        这里只做状态收敛，不执行 Graph，也不把消息命令重新放回消费队列。
        用户显式调用恢复接口后，``resume_run`` 才会释放原消息命令。
        """
        recoverable_statuses = tuple(
            status.value
            for status in (
                AgentRunStatus.QUEUED,
                AgentRunStatus.RUNNING,
                AgentRunStatus.PAUSED,
                AgentRunStatus.WAITING_APPROVAL,
                AgentRunStatus.CANCEL_REQUESTED,
            )
        )
        now = _now()
        async with get_core_session() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            try:
                run_rows = (
                    await db.execute(
                        select(AgentRunModel).where(
                            AgentRunModel.status.in_(recoverable_statuses)
                        )
                    )
                ).scalars().all()
                active_runs = [
                    row
                    for row in run_rows
                    if row.status != AgentRunStatus.CANCEL_REQUESTED.value
                    and not row.cancel_requested
                ]
                run_ids = [row.run_id for row in active_runs]
                session_ids = {row.session_id for row in active_runs}
                paused = 0
                if run_ids:
                    changed = await db.execute(
                        update(AgentRunModel)
                        .where(AgentRunModel.run_id.in_(run_ids))
                        .values(
                            status=AgentRunStatus.PAUSED.value,
                            pause_requested=1,
                            updated_at=now,
                        )
                    )
                    paused = int(changed.rowcount or 0)
                if session_ids:
                    await db.execute(
                        update(SessionModel)
                        .where(SessionModel.id.in_(session_ids))
                        .values(status="interrupted", updated_at=now)
                    )

                held = 0
                if run_ids:
                    changed = await db.execute(
                        update(AgentCommandModel)
                        .where(
                            AgentCommandModel.run_id.in_(run_ids),
                            AgentCommandModel.command_type
                            == CommandType.MESSAGE_SUBMIT.value,
                            AgentCommandModel.status.in_(
                                (
                                    AgentCommandStatus.PENDING.value,
                                    AgentCommandStatus.CLAIMED.value,
                                )
                            ),
                        )
                        .values(
                            status=AgentCommandStatus.CLAIMED.value,
                            claimed_at=now,
                        )
                    )
                    held = int(changed.rowcount or 0)
                await db.commit()
                return {"paused_runs": paused, "held_commands": held}
            except Exception:
                await db.rollback()
                raise

    async def resume_run(self, run_id: str) -> bool:
        """在用户明确恢复后释放原消息命令。

        参数:
            run_id (str): 待恢复的 Root Run 标识。
        返回值:
            bool: 运行存在且完成恢复状态转换时返回 ``True``。
        异常:
            数据库写入失败时传播 SQLAlchemy 异常。
        """
        now = _now()
        should_notify = False
        async with get_core_session() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            try:
                run = await db.get(AgentRunModel, run_id)
                if run is None:
                    await db.commit()
                    return False
                run.status = AgentRunStatus.RUNNING.value
                run.pause_requested = 0
                run.updated_at = now
                await db.execute(
                    update(SessionModel)
                    .where(SessionModel.id == run.session_id)
                    .values(status="running", updated_at=now)
                )
                command = await db.scalar(
                    select(AgentCommandModel).where(
                        AgentCommandModel.run_id == run_id,
                        AgentCommandModel.command_type
                        == CommandType.MESSAGE_SUBMIT.value,
                        AgentCommandModel.status.in_(
                            (
                                AgentCommandStatus.PENDING.value,
                                AgentCommandStatus.CLAIMED.value,
                            )
                        ),
                    )
                )
                if command is not None:
                    command.status = AgentCommandStatus.PENDING.value
                    command.available_at = now
                    command.claimed_at = None
                    should_notify = True
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        if should_notify and self.command_notifier is not None:
            await self.command_notifier.notify()
        return True

    async def mark_running_tool_calls_unknown(self) -> int:
        """在启动阶段把上一个进程遗留的工具执行标记为未知。

        参数:
            无。
        返回值:
            int: 被标记的工具尝试数量。
        异常:
            数据库写入失败时传播 SQLAlchemy 异常。
        """
        rows = (
            await self._tool_call_rows_with_status("running")
        )
        if not rows:
            return 0
        async with get_core_session() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            try:
                changed = await db.execute(
                    update(ToolCallModel)
                    .where(ToolCallModel.status == "running")
                    .values(status="unknown")
                )
                for row in rows:
                    if row.step_id:
                        await db.execute(
                            update(StepModel)
                            .where(StepModel.id == row.step_id)
                            .values(status="unknown")
                        )
                await db.commit()
                return int(changed.rowcount or 0)
            except Exception:
                await db.rollback()
                raise

    async def _tool_call_rows_with_status(self, status: str) -> list[ToolCallModel]:
        """读取指定状态的工具账本行，供启动状态收敛使用。"""
        async with get_core_session() as db:
            result = await db.execute(
                select(ToolCallModel).where(ToolCallModel.status == status)
            )
            return list(result.scalars())

    async def create_worker_run(
        self,
        *,
        run_id: str,
        session_id: str,
        parent_run_id: str,
        root_run_id: str,
        plan_id: str | None,
        task_id: str | None,
        attempt: int,
    ) -> None:
        """创建独立 Worker Run，不改变会话当前 Root Run。

        参数:
            run_id: 本次 Worker 尝试的唯一标识。
            session_id: Worker 所属用户会话。
            parent_run_id: 创建该 Worker 的直接父运行。
            root_run_id: 整个编排请求的 Root Run。
            plan_id: 所属计划；旧委派路径可以为空。
            task_id: 稳定任务标识；旧委派路径可以为空。
            attempt: 同一任务从 1 开始的尝试序号。

        返回值:
            None: 记录成功提交。

        异常:
            ValueError: 标识为空、尝试序号非法或 run_id 已存在。
        """

        if not all((run_id, session_id, parent_run_id, root_run_id)):
            raise ValueError("worker run identity fields must not be empty")
        if attempt < 1:
            raise ValueError("worker run attempt must be at least 1")
        async with get_core_session() as db:
            if await db.get(AgentRunModel, run_id) is not None:
                raise ValueError(ErrorDetail.RUN_ID_CONFLICT)
            db.add(
                AgentRunModel(
                    run_id=run_id,
                    session_id=session_id,
                    parent_run_id=parent_run_id,
                    root_run_id=root_run_id,
                    role="worker",
                    plan_id=plan_id,
                    task_id=task_id,
                    attempt=attempt,
                    depth=1,
                    status=AgentRunStatus.QUEUED.value,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
            await db.commit()

    async def update_run_status(
        self, run_id: str, status: AgentRunStatus, error: str | None = None
    ) -> None:
        """更新运行状态并记录可选错误信息。

        参数:
            run_id (str): 运行 ID。
            status (AgentRunStatus): 目标运行状态。
            error (str | None): 可选错误文本。
        返回值:
            None: 运行不存在时也保持幂等。
        异常:
            数据库写入失败时传播 SQLAlchemy 异常。
        """
        async with get_core_session() as db:
            row = await db.get(AgentRunModel, run_id)
            if row:
                # A pause/cancel control can be consumed while the message
                # task is transitioning QUEUED -> RUNNING. Never let that
                # late transition erase the durable control state.
                effective_status = status
                if status == AgentRunStatus.RUNNING and row.status in {
                    AgentRunStatus.PAUSED.value,
                    AgentRunStatus.CANCEL_REQUESTED.value,
                }:
                    effective_status = AgentRunStatus(row.status)
                row.status, row.error, row.updated_at = effective_status.value, error, _now()
                session_status = {
                    AgentRunStatus.RUNNING: "running",
                    AgentRunStatus.PAUSED: "interrupted",
                    AgentRunStatus.CANCEL_REQUESTED: "interrupted",
                    AgentRunStatus.CANCELLED: "interrupted",
                    AgentRunStatus.COMPLETED: "idle",
                    AgentRunStatus.FAILED: "failed",
                }.get(effective_status)
                if session_status and row.parent_run_id is None:
                    await db.execute(
                        update(SessionModel)
                        .where(SessionModel.id == row.session_id)
                        .values(status=session_status, updated_at=_now())
                    )
                await db.commit()

    async def update_run_control(
        self,
        run_id: str,
        *,
        pause: bool = False,
        clear_pause: bool = False,
        cancel: bool = False,
        status: AgentRunStatus | None = None,
    ) -> bool:
        """更新运行的暂停、取消控制标志和可选状态。

        参数:
            run_id (str): 运行 ID。
            pause (bool): 是否设置暂停请求标志。
            clear_pause (bool): 是否清除暂停请求标志。
            cancel (bool): 是否设置取消请求标志。
            status (AgentRunStatus | None): 可选的新状态。
        返回值:
            bool: 找到并更新运行时返回 ``True``，否则返回 ``False``。
        异常:
            数据库写入失败时传播 SQLAlchemy 异常。
        """
        async with get_core_session() as db:
            row = await db.get(AgentRunModel, run_id)
            if row is None:
                return False
            if pause:
                row.pause_requested = 1
            if clear_pause:
                row.pause_requested = 0
            if cancel:
                row.cancel_requested = 1
            if status:
                row.status = status.value
            row.updated_at = _now()
            session_status = {
                AgentRunStatus.RUNNING: "running",
                AgentRunStatus.PAUSED: "interrupted",
                AgentRunStatus.CANCEL_REQUESTED: "interrupted",
            }.get(status) if status else None
            if session_status and row.parent_run_id is None:
                await db.execute(
                    update(SessionModel)
                    .where(SessionModel.id == row.session_id)
                    .values(status=session_status, updated_at=row.updated_at)
                )
            await db.commit()
            return True

    async def list_runs_for_session(self, session_id: str) -> list[AgentRunModel]:
        """按创建时间返回会话的全部运行记录。

        参数:
            session_id (str): 非空会话 ID。
        返回值:
            list[AgentRunModel]: 按创建时间升序排列的运行记录。
        异常:
            数据库查询失败时传播 SQLAlchemy 异常。
        """
        async with get_core_session() as db:
            result = await db.execute(
                select(AgentRunModel)
                .where(AgentRunModel.session_id == session_id)
                .order_by(AgentRunModel.created_at)
            )
            return list(result.scalars())

    async def get_run(self, run_id: str) -> AgentRunModel | None:
        """按主键查询运行记录。

        参数:
            run_id (str): 运行主键。
        返回值:
            AgentRunModel | None: 匹配记录；不存在时返回 ``None``。
        异常:
            数据库查询失败时传播 SQLAlchemy 异常。
        """
        async with get_core_session() as db:
            return await db.get(AgentRunModel, run_id)

    async def list_recoverable_runs(self) -> list[AgentRunRecord]:
        """返回启动恢复需要处理的运行，包括暂停中的运行。

        返回值:
            list[AgentRunRecord]: 状态为排队、运行、暂停或待取消的轻量记录。
        异常:
            数据库查询或状态值转换失败时传播相应异常。
        """
        async with get_core_session() as db:
            result = await db.execute(
                select(AgentRunModel).where(
                    AgentRunModel.status.in_(
                        status.value
                        for status in (
                            AgentRunStatus.QUEUED,
                            AgentRunStatus.RUNNING,
                            AgentRunStatus.PAUSED,
                            AgentRunStatus.WAITING_APPROVAL,
                            AgentRunStatus.CANCEL_REQUESTED,
                        )
                    )
                )
            )
            return [
                AgentRunRecord(
                    run_id=row.run_id,
                    status=AgentRunStatus(row.status),
                    cancel_requested=row.cancel_requested,
                )
                for row in result.scalars()
            ]

    async def claim_next_command(self) -> AgentCommandRecord | None:
        """领取一条可执行命令并标记为处理中。

        返回值:
            AgentCommandRecord | None: 成功领取的命令；没有可执行命令时返回 ``None``。
        异常:
            数据库竞争或状态转换失败时传播 SQLAlchemy 异常。
        """
        async with get_core_session() as db:
            # Serialize selection and mutation across workers. Without an
            # immediate write lock two consumers can read the same pending row
            # before either one commits its CLAIMED update.
            await db.execute(text("BEGIN IMMEDIATE"))
            try:
                row = await db.scalar(
                    select(AgentCommandModel)
                    .where(
                        AgentCommandModel.status == AgentCommandStatus.PENDING.value,
                        AgentCommandModel.available_at <= _now(),
                    )
                    .order_by(AgentCommandModel.issued_at)
                    .limit(1)
                )
                if row is None:
                    await db.commit()
                    return None
                row.status = AgentCommandStatus.CLAIMED.value
                row.attempt += 1
                row.claimed_at = _now()
                await db.commit()
                return AgentCommandRecord(
                    command_id=row.command_id,
                    session_id=row.session_id,
                    run_id=row.run_id,
                    command_type=CommandType(row.command_type),
                    schema_version=row.schema_version,
                    payload_json=row.payload_json,
                )
            except Exception:
                await db.rollback()
                raise

    async def complete_command(
        self,
        command_id: str,
        *,
        status: AgentCommandStatus,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """完成命令并写入结果或错误。

        参数:
            command_id (str): 命令 ID。
            status (AgentCommandStatus): 命令终态。
            result (dict[str, Any] | None): 可选成功结果。
            error (dict[str, Any] | None): 可选错误结构。
        返回值:
            None: 命令不存在时保持幂等。
        异常:
            结果无法序列化或数据库写入失败时传播相应异常。
        """
        async with get_core_session() as db:
            row = await db.get(AgentCommandModel, command_id)
            if row is None:
                return
            row.status = status.value
            row.result_json = _json_dumps(result) if result is not None else None
            row.error_json = _json_dumps(error) if error is not None else None
            row.claimed_at = None
            await db.commit()

    async def reclaim_stale_commands(self, lease_seconds: int = 300) -> int:
        """Return commands left claimed by a crashed worker to the queue."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)
        ).isoformat()
        async with get_core_session() as db:
            result = await db.execute(
                text("""UPDATE agent_commands
                    SET status = :pending, available_at = :now, claimed_at = NULL
                    WHERE status = :claimed
                      AND (claimed_at IS NULL OR claimed_at < :cutoff)"""),
                {
                    "pending": AgentCommandStatus.PENDING.value,
                    "claimed": AgentCommandStatus.CLAIMED.value,
                    "now": _now(),
                    "cutoff": cutoff,
                },
            )
            await db.commit()
            return int(result.rowcount or 0)

    async def get_command(self, command_id: str) -> AgentCommandModel | None:
        """按 ID 查询命令记录。

        参数:
            command_id (str): 命令主键。
        返回值:
            AgentCommandModel | None: 匹配记录；不存在时返回 ``None``。
        异常:
            数据库查询失败时传播 SQLAlchemy 异常。
        """
        async with get_core_session() as db:
            return await db.get(AgentCommandModel, command_id)

    async def list_events_after(
        self, session_id: str, after: int = 0
    ) -> list[AgentEventModel]:
        """查询会话中游标之后的持久化事件。

        参数:
            session_id (str): 会话 ID。
            after (int): 排除该 session_seq 及之前事件，必须为非负整数。
        返回值:
            list[AgentEventModel]: 按 session_seq 升序排列的事件。
        异常:
            数据库查询失败时传播 SQLAlchemy 异常。
        """
        async with get_core_session() as db:
            result = await db.execute(
                select(AgentEventModel)
                .where(
                    AgentEventModel.session_id == session_id,
                    AgentEventModel.session_seq > after,
                )
                .order_by(AgentEventModel.session_seq)
            )
            return list(result.scalars())

    async def list_events_between(
        self, session_id: str, after: int = 0, upto: int | None = None
    ) -> list[AgentEventModel]:
        """查询指定会话游标区间内的事件，供 SSE watermark 重放使用。"""
        async with get_core_session() as db:
            query = select(AgentEventModel).where(
                AgentEventModel.session_id == session_id,
                AgentEventModel.session_seq > after,
            )
            if upto is not None:
                query = query.where(AgentEventModel.session_seq <= upto)
            result = await db.execute(query.order_by(AgentEventModel.session_seq))
            return list(result.scalars())

    async def open_subscription(
        self, session_id: str
    ) -> tuple[asyncio.Queue[ApplicationEvent], int]:
        """登记订阅并原子取得历史重放 watermark。"""
        if self.transport is None:
            raise RuntimeError("Realtime transport is not configured")
        async with self._event_locks[session_id]:
            queue = await self.transport.open_subscription(session_id)
            async with get_core_session() as db:
                watermark = await self._cached_session_seq(db, session_id)
            return queue, watermark

    async def list_snapshots_for_session(
        self, session_id: str
    ) -> list[StreamSnapshotModel]:
        """查询会话的流快照，并按更新时间升序返回。

        参数:
            session_id (str): 会话 ID。
        返回值:
            list[StreamSnapshotModel]: 会话快照列表。
        异常:
            数据库查询失败时传播 SQLAlchemy 异常。
        """
        async with get_core_session() as db:
            result = await db.execute(
                select(StreamSnapshotModel)
                .where(StreamSnapshotModel.session_id == session_id)
                .order_by(StreamSnapshotModel.updated_at)
            )
            return list(result.scalars())

    @staticmethod
    def _same_command(
        existing: AgentCommandModel, command: Command, payload_hash: str
    ) -> bool:
        """判断数据库记录是否与待入队命令代表同一客户端意图。

        ``MESSAGE_SUBMIT`` 的 ``run_id`` 由服务端生成，重试请求可能携带新的候选值，
        因此该字段不参与消息命令的幂等比较；其它命令的目标运行必须保持一致。
        """
        same_identity = (
            existing.payload_hash == payload_hash
            and existing.command_type == command.command_type.value
            and existing.session_id == command.session_id
            and existing.schema_version == command.schema_version
        )
        if not same_identity:
            return False
        return command.command_type == CommandType.MESSAGE_SUBMIT or (
            existing.run_id == command.run_id
        )

    @staticmethod
    def _reuse_or_conflict(
        existing: AgentCommandModel, command: Command, payload_hash: str
    ) -> bool:
        """复用相同命令的持久化运行 ID，或抛出命令冲突异常。"""
        if not AgentStore._same_command(existing, command, payload_hash):
            raise ValueError(ErrorDetail.COMMAND_ID_CONFLICT)
        command.run_id = existing.run_id
        return False

    async def enqueue_command(self, command: Command) -> bool:
        """校验并幂等入队命令，必要时原子创建运行记录。

        参数:
            command (Command): 已构造的版本化命令；payload 必须符合命令协议。
        返回值:
            bool: 新命令已插入时返回 ``True``，重复且内容一致时返回 ``False``。
        异常:
            ValueError: payload 非法、命令 ID 冲突或会话已有不可并行运行。
            数据库异常: 持久化失败且无法恢复时传播 SQLAlchemy 异常。
        """
        command.validate_payload()
        payload_hash = command.payload_fingerprint()
        payload_json = json.dumps(
            command.payload.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
        )
        async with get_core_session() as db:
            lock_timeout_changed = False
            try:
                existing = await db.get(AgentCommandModel, command.command_id)
                if existing:
                    return AgentStore._reuse_or_conflict(
                        existing, command, payload_hash
                    )

                if command.command_type == CommandType.MESSAGE_SUBMIT:
                    # 只有消息命令需要竞争会话的 Run 创建权；控制命令可以直接入队。
                    try:
                        await db.rollback()
                        await db.execute(text("PRAGMA busy_timeout = 0"))
                        lock_timeout_changed = True
                        await db.execute(text("BEGIN IMMEDIATE"))
                    except OperationalError as exc:
                        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                            raise ValueError(ErrorDetail.SESSION_BUSY) from exc
                        raise

                    # 首次快速查重未命中时，必须在写锁内再次查重，避免同一命令的
                    # 并发重试被错误识别为 session_busy。
                    existing = await db.get(AgentCommandModel, command.command_id)
                    if existing:
                        return AgentStore._reuse_or_conflict(
                            existing, command, payload_hash
                        )

                    active = await db.scalar(
                        select(AgentRunModel)
                        .where(
                            AgentRunModel.session_id == command.session_id,
                            AgentRunModel.parent_run_id.is_(None),
                            AgentRunModel.status.in_(
                                status.value
                                for status in (
                                    AgentRunStatus.QUEUED,
                                    AgentRunStatus.RUNNING,
                                    AgentRunStatus.CANCEL_REQUESTED,
                                    AgentRunStatus.WAITING_APPROVAL,
                                )
                            ),
                        )
                        .limit(1)
                    )
                    if active:
                        raise ValueError(ErrorDetail.SESSION_BUSY)

                    # 暂停只影响原 Run；新消息必须创建独立 Run，避免重新唤醒或覆盖旧 Run。
                    run_id = command.run_id or generate_time_id()
                    if await db.get(AgentRunModel, run_id):
                        raise ValueError(ErrorDetail.RUN_ID_CONFLICT)
                    db.add(
                        AgentRunModel(
                            run_id=run_id,
                            session_id=command.session_id,
                            created_by_command_id=command.command_id,
                            root_run_id=run_id,
                            role="root",
                            depth=0,
                            status=AgentRunStatus.QUEUED.value,
                            created_at=_now(),
                            updated_at=_now(),
                        )
                    )
                    command.run_id = run_id

                row = AgentCommandModel(
                    command_id=command.command_id,
                    session_id=command.session_id,
                    run_id=command.run_id,
                    command_type=command.command_type.value,
                    schema_version=command.schema_version,
                    payload_json=payload_json,
                    payload_hash=payload_hash,
                    available_at=_now(),
                    issued_at=command.issued_at.isoformat(),
                )
                try:
                    db.add(row)
                    await db.commit()
                    if self.command_notifier is not None:
                        await self.command_notifier.notify()
                    return True
                except IntegrityError:
                    await db.rollback()
                    existing = await db.get(AgentCommandModel, command.command_id)
                    if existing:
                        return AgentStore._reuse_or_conflict(
                            existing, command, payload_hash
                        )
                    # 不是命令主键冲突时保留原始数据库异常，避免误报为命令冲突。
                    raise
            finally:
                if lock_timeout_changed:
                    if db.in_transaction():
                        await db.rollback()
                    await db.execute(
                        text(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
                    )

    async def publish(self, event: ApplicationEvent) -> ApplicationEvent:
        """按会话串行分配游标，持久化事件后广播。

        参数:
            event (ApplicationEvent): 要发布的事件。
        返回值:
            ApplicationEvent: 带最终 session_seq 和 Chunk 序号的事件对象。
        异常:
            事件 payload 无法序列化或数据库、实时传输失败时传播相应异常。
        """
        async with self._event_locks[event.session_id]:
            async with get_core_session() as db:
                # 进程内按会话串行，事务锁覆盖多进程/多实例下的 SQLite 竞争。
                await db.execute(text("BEGIN IMMEDIATE"))
                payload = {
                    key: value
                    for key, value in event.payload.items()
                    if value is not None
                }
                payload_json = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                )

                current = await self._cached_session_seq(db, event.session_id)
                stream_cache_key = (
                    (event.session_id, event.stream_id)
                    if event.stream_id is not None
                    else None
                )
                if (
                    stream_cache_key is not None
                    and event.event_type == EventType.LLM_TOKEN
                    and event.chunk_id is None
                ):
                    await self._cached_stream_chunk_id(
                        db, stream_cache_key[0], stream_cache_key[1]
                    )

                # stream_id + chunk_id 是流事件的幂等键。重复发布不再次广播，
                # 内容变化则拒绝，避免客户端出现不可诊断的分叉流。
                if event.stream_id is not None and event.chunk_id is not None:
                    existing = await db.scalar(
                        select(AgentEventModel).where(
                            AgentEventModel.session_id == event.session_id,
                            AgentEventModel.stream_id == event.stream_id,
                            AgentEventModel.chunk_id == event.chunk_id,
                        )
                    )
                    if existing is not None:
                        same = (
                            existing.event_type == event.event_type
                            and existing.run_id == event.run_id
                            and existing.message_id == event.message_id
                            and existing.attachment_id == event.attachment_id
                            and existing.payload_json == payload_json
                            and existing.is_complete == int(event.is_complete)
                        )
                        if not same:
                            raise ValueError("stream chunk idempotency conflict")
                        return event.model_copy(
                            update={
                                "session_seq": existing.session_seq,
                                "chunk_id": existing.chunk_id,
                            }
                        )

                # Durable lifecycle events use a business transition key. A
                # replay with the same key must return the original cursor and
                # must not consume another session sequence number.
                if event.transition_id is not None:
                    existing = await db.scalar(
                        select(AgentEventModel).where(
                            AgentEventModel.session_id == event.session_id,
                            AgentEventModel.transition_id == event.transition_id,
                        )
                    )
                    if existing is not None:
                        same = (
                            existing.event_type == event.event_type
                            and existing.run_id == event.run_id
                            and existing.message_id == event.message_id
                            and existing.attachment_id == event.attachment_id
                            and existing.payload_json == payload_json
                        )
                        if not same:
                            raise ValueError("event transition idempotency conflict")
                        return event.model_copy(
                            update={"session_seq": existing.session_seq}
                        )

                chunk_id = event.chunk_id
                if (
                    event.stream_id
                    and event.event_type == EventType.LLM_TOKEN
                    and chunk_id is None
                ):
                    chunk_id = (
                        self._stream_chunk_cache[(event.session_id, event.stream_id)]
                        + 1
                    )
                session_seq = current + 1
                row = AgentEventModel(
                    session_id=event.session_id,
                    session_seq=session_seq,
                    run_id=event.run_id,
                    message_id=event.message_id,
                    attachment_id=event.attachment_id,
                    event_type=event.event_type,
                    durability=event.durability.value,
                    stream_id=event.stream_id,
                    stream_type=event.stream_type,
                    chunk_id=chunk_id,
                    is_complete=int(event.is_complete),
                    parent_run_id=event.parent_run_id,
                    transition_id=event.transition_id,
                    payload_json=payload_json,
                    occurred_at=event.occurred_at.isoformat(),
                )
                db.add(row)
                await db.commit()
                self._session_seq_cache[event.session_id] = session_seq
                if stream_cache_key is not None and chunk_id is not None:
                    self._advance_stream_chunk_cache(stream_cache_key, chunk_id)
                persisted = event.model_copy(
                    update={
                        "session_seq": session_seq,
                        "chunk_id": chunk_id,
                    }
                )
                if self.transport is not None:
                    # 只有提交成功后才广播；队列满时在这里背压，不丢事件。
                    await self.transport.publish(persisted)
                return persisted

    async def publish_realtime(self, event: ApplicationEvent) -> ApplicationEvent:
        """只广播实时事件，不写入 ``agent_events`` 或分配 ``session_seq``。

        参数:
            event (ApplicationEvent): 不需要断线重放的实时事件。
        返回值:
            ApplicationEvent: 原事件，保持无会话游标的实时语义。
        异常:
            实时队列写入失败时传播底层异步异常。
        """
        if event.durability != EventDurability.REALTIME:
            raise ValueError("publish_realtime only accepts realtime events")
        if self.transport is not None:
            await self.transport.publish(event)
        return event

    async def upsert_snapshot(
        self,
        session_id: str,
        stream_id: str,
        version: int,
        content: str,
        *,
        run_id: str | None = None,
        stream_type: str = "answer",
        last_chunk_id: int = 0,
        status: StreamSnapshotStatus = StreamSnapshotStatus.STREAMING,
    ) -> bool:
        """按版本递增条件写入流快照。

        参数:
            session_id (str): 快照所属会话 ID。
            stream_id (str): 流快照主键。
            version (int): 新快照版本，必须高于已保存版本。
            content (str): 当前完整文本内容。
            run_id (str | None): 可选关联运行 ID。
            status (StreamSnapshotStatus): 快照状态。
        返回值:
            bool: 实际写入新版本时返回 ``True``，版本未增长时返回 ``False``。
        异常:
            数据库写入失败时传播 SQLAlchemy 异常。
        """
        async with get_core_session() as db:
            row = await db.get(StreamSnapshotModel, stream_id)
            if row and row.version >= version:
                return False
            if row is None:
                row = StreamSnapshotModel(
                    stream_id=stream_id,
                    session_id=session_id,
                    stream_type=stream_type,
                    version=version,
                    last_chunk_id=last_chunk_id,
                    content=content,
                    content_byte_length=len(content.encode("utf-8")),
                    status=status.value,
                    updated_at=_now(),
                )
                db.add(row)
            if last_chunk_id == 0:
                last_chunk_id = await self._cached_stream_chunk_id(
                    db, session_id, stream_id
                )
            (
                row.run_id,
                row.stream_type,
                row.version,
                row.last_chunk_id,
                row.content,
                row.content_byte_length,
                row.status,
                row.updated_at,
            ) = (
                run_id,
                stream_type,
                version,
                last_chunk_id,
                content,
                len(content.encode("utf-8")),
                status.value,
                _now(),
            )
            await db.commit()
            return True

    async def create_approval(
        self,
        *,
        approval_id: str,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        risk_level: RiskLevel,
        plan_id: str | None = None,
        task_id: str | None = None,
        worker_run_id: str | None = None,
        expires_at: str | None = None,
    ) -> bool:
        """创建待审批记录并持久化工具参数。

        参数:
            approval_id (str): 审批 ID。
            session_id (str): 会话 ID。
            run_id (str): 运行 ID。
            tool_call_id (str): 工具调用 ID。
            tool_name (str): 工具名称。
            arguments (dict): 工具参数，可 JSON 序列化。
            risk_level (RiskLevel): 工具风险等级。
            expires_at (str | None): 审批截止时间；恢复任务时沿用原截止时间。
        返回值:
            bool: 首次插入返回 ``True``；同一工具尝试已有记录时返回 ``False``。
        异常:
            参数无法序列化或数据库约束不满足时传播相应异常。
        """
        async with get_core_session() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            try:
                existing = await db.scalar(
                    select(ApprovalRecordModel).where(
                        ApprovalRecordModel.tool_call_id == tool_call_id
                    )
                )
                if existing is not None:
                    await db.commit()
                    return False
                db.add(
                    ApprovalRecordModel(
                        approval_id=approval_id,
                        session_id=session_id,
                        run_id=run_id,
                        plan_id=plan_id,
                        task_id=task_id,
                        worker_run_id=worker_run_id,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        arguments_json=_json_dumps(arguments),
                        risk_level=risk_level.value,
                        created_at=_now(),
                        expires_at=expires_at,
                    )
                )
                await db.commit()
            except IntegrityError:
                await db.rollback()
                existing = await db.scalar(
                    select(ApprovalRecordModel).where(
                        ApprovalRecordModel.tool_call_id == tool_call_id
                    )
                )
                if existing is None:
                    raise
                return False
            except Exception:
                await db.rollback()
                raise
            return True

    async def get_approval(self, approval_id: str) -> ApprovalRecordModel | None:
        """按 ID 查询审批记录。

        参数:
            approval_id (str): 审批主键。
        返回值:
            ApprovalRecordModel | None: 匹配记录；不存在时返回 ``None``。
        异常:
            数据库查询失败时传播 SQLAlchemy 异常。
        """
        async with get_core_session() as db:
            return await db.get(ApprovalRecordModel, approval_id)

    async def get_approval_for_tool_call(
        self, tool_call_id: str
    ) -> ApprovalRecordModel | None:
        """按工具尝试 ID 查询其审批记录。

        参数:
            tool_call_id (str): 工具尝试账本 ID。
        返回值:
            ApprovalRecordModel | None: 已有关联审批时返回记录，否则返回 ``None``。
        异常:
            数据库查询失败时传播 SQLAlchemy 异常。
        """
        async with get_core_session() as db:
            return await db.scalar(
                select(ApprovalRecordModel)
                .where(ApprovalRecordModel.tool_call_id == tool_call_id)
                .order_by(ApprovalRecordModel.created_at.desc())
                .limit(1)
            )

    async def resolve_approval_for_attempt(
        self,
        approval_id: str,
        decision: AgentApprovalDecision,
        *,
        expected_run_id: str | None = None,
        expected_worker_run_id: str | None = None,
        expected_task_id: str | None = None,
        expected_plan_id: str | None = None,
    ) -> bool:
        """原子解析审批，并校验其仍属于预期的编排尝试。

        参数:
            approval_id: 审批主键。
            decision: 用户或系统作出的审批决定。
            expected_run_id: 当前期望的 Root/Worker 运行标识。
            expected_worker_run_id: 当前期望的 Worker 尝试标识。
            expected_task_id: 当前期望的稳定任务标识。
            expected_plan_id: 当前期望的执行计划标识。

        返回值:
            bool: 审批仍为待处理且运行归属匹配时返回 True；否则返回 False。

        异常:
            数据库写入失败时传播 SQLAlchemy 异常。
        """
        async with get_core_session() as db:
            row = await db.get(ApprovalRecordModel, approval_id)
            if row is None or row.status != AgentApprovalStatus.PENDING.value:
                return False
            if expected_run_id and row.run_id != expected_run_id:
                return False
            if expected_plan_id and row.plan_id != expected_plan_id:
                return False
            if expected_task_id and row.task_id != expected_task_id:
                return False
            if expected_worker_run_id and row.worker_run_id != expected_worker_run_id:
                return False
            row.status = AgentApprovalStatus.RESOLVED.value
            row.decision = decision.value
            row.decided_at = _now()
            await db.commit()
            return True

    async def list_pending_approvals(
        self, session_id: str | None = None
    ) -> list[ApprovalRecordModel]:
        """查询待审批记录，可按会话过滤。

        参数:
            session_id (str | None): 可选会话 ID；为空时返回全部待审批记录。
        返回值:
            list[ApprovalRecordModel]: 按创建时间升序排列的记录。
        异常:
            数据库查询失败时传播 SQLAlchemy 异常。
        """
        async with get_core_session() as db:
            query = select(ApprovalRecordModel).where(
                ApprovalRecordModel.status == AgentApprovalStatus.PENDING.value
            )
            if session_id:
                query = query.where(ApprovalRecordModel.session_id == session_id)
            result = await db.execute(query.order_by(ApprovalRecordModel.created_at))
            return list(result.scalars())

    async def resolve_approval(
        self, approval_id: str, decision: AgentApprovalDecision
    ) -> bool:
        """原子地将待审批记录解析为已处理状态。

        参数:
            approval_id (str): 审批 ID。
            decision (AgentApprovalDecision): 审批决定。
        返回值:
            bool: 成功解析待处理记录时返回 ``True``；不存在或已解析时返回 ``False``。
        异常:
            数据库写入失败时传播 SQLAlchemy 异常。
        """
        async with get_core_session() as db:
            row = await db.scalar(
                select(ApprovalRecordModel).where(
                    ApprovalRecordModel.approval_id == approval_id,
                    ApprovalRecordModel.status == AgentApprovalStatus.PENDING.value,
                )
            )
            if row is None:
                return False
            row.status = AgentApprovalStatus.RESOLVED.value
            row.decision = decision.value
            row.decided_at = _now()
            await db.commit()
            return True
