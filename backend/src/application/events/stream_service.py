"""会话事件历史重放和实时订阅用例。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime

from application.observability import ProgressEntry, ProgressSource, RunObservabilityService
from domain.events import ApplicationEvent, EventPort
from domain.sessions import SessionRepository
from application.common.responses import EventResponse
from application.sessions.history_port import (
    ActiveSessionSnapshot,
    SessionHistoryQueryPort,
    SessionTurnSnapshot,
)
from .assistant_hub import AssistantPrefix, AssistantStreamHub

STREAM_SNAPSHOT_PAGE_SIZE = 20
_ACTIVE_RUN_STATUSES = frozenset({"queued", "running", "stopping"})


class EventSessionNotFoundError(LookupError):
    """事件订阅目标会话不存在。"""


class EventStreamService:
    """组合会话权限、事件 watermark 和订阅生命周期。"""

    def __init__(
        self,
        sessions: SessionRepository,
        events: EventPort,
        history_reader: SessionHistoryQueryPort | None = None,
        observability: RunObservabilityService | None = None,
        assistant_streams: AssistantStreamHub | None = None,
    ) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `sessions`（SessionRepository）：读取会话身份和状态的仓储依赖；约束：无额外运行时限制。
        - `events`（EventPort）：用于读取、写入或推送的事件存储依赖；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._sessions = sessions
        self._events = events
        self._history_reader = history_reader
        self._observability = observability
        self._assistant_streams = assistant_streams

    async def ensure_session(self, session_id: str) -> None:
        """确认会话。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：须通过方法内校验；失败条件见下方异常说明。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - EventSessionNotFoundError：校验条件 `await self._sessions.get(session_id) is None` 成立时。
        - 未列出的依赖异常按当前实现传播。
        """
        if await self._sessions.get(session_id) is None:
            raise EventSessionNotFoundError(session_id)

    async def open_subscription(
        self, session_id: str
    ) -> tuple[asyncio.Queue[ApplicationEvent], int]:
        """为会话注册事件订阅，并返回队列和当前事件游标。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。

        返回：
        - tuple[asyncio.Queue[ApplicationEvent], int]：事件队列及订阅建立时的最新事件游标。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        await self.ensure_session(session_id)
        return await self._events.open_subscription(session_id)

    async def replay(
        self, session_id: str, after: int, upto: int | None, limit: int = 256
    ) -> list[EventResponse]:
        """按游标和上界读取会话事件。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `after`（int）：分页或事件回放的起始位置；具体边界语义由对应查询定义；约束：无额外运行时限制。
        - `upto`（int | None）：事件回放的结束游标边界；约束：允许为 None，按未提供值的分支处理。
        - `limit`（int）：本次读取或处理的数量上限；约束：可省略，默认值为 `256`。

        返回：
        - list[EventResponse]：符合会话、游标范围和数量上限的事件响应列表。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return [
            _event_response(value)
            for value in await self._events.list_events_between(
                session_id, after, upto, limit=limit
            )
        ]

    @staticmethod
    def response(value: ApplicationEvent) -> EventResponse:
        """将应用事件转换为对外响应对象。

        参数：
        - `value`（ApplicationEvent）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - EventResponse：供调用方展示或继续处理的 `EventResponse` 响应对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return _event_response(value)

    async def close(
        self, session_id: str, queue: asyncio.Queue[ApplicationEvent]
    ) -> None:
        """取消会话事件订阅并移除对应队列。

        参数：
        - `session_id`（str）：目标会话的稳定标识，用于限定数据范围；约束：无额外运行时限制。
        - `queue`（asyncio.Queue[ApplicationEvent]）：保存待推送事件的队列；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        await self._events.close_subscription(session_id, queue)

    async def snapshot(
        self,
        session_id: str,
        *,
        after_event_sequence: int = 0,
        after_progress_ingest_sequence: int = 0,
        after_run_sequence: int | None = None,
        active_run_id: str | None = None,
        include_latest_if_empty: bool = False,
        limit: int = STREAM_SNAPSHOT_PAGE_SIZE,
    ) -> tuple[
        dict[str, object],
        ActiveSessionSnapshot | None,
        dict[str, AssistantPrefix],
        str,
    ]:
        """在订阅已建立后读取水位之后的完整轮次集合。

        参数：
        - `session_id`：目标会话。
        - `after_event_sequence`：客户端 messages 响应已消费的事件水位。
        - `after_progress_ingest_sequence`：客户端 messages 响应已消费的 Progress 水位。
        - `after_run_sequence`：客户端 messages 响应已消费的最后一轮 Run 序号。
        - `active_run_id`：messages 或 SSE 令牌拥有的活动 Run 边界标识。
        - `include_latest_if_empty`：首次手动建连时允许返回最新 Run。
        - `limit`：单页最多包含的完整 Run 数，避免离线恢复快照无界增长。

        返回：
        - 快照内容、一致性读取结果、当前 Run 的内存 assistant 前缀及快照令牌。

        异常：
        - RuntimeError：聊天快照依赖未注入。
        - 数据库异常：历史读取失败时原样传播。
        """
        if self._history_reader is None or self._observability is None:
            raise RuntimeError("chat_stream_snapshot_unavailable")
        active = await self._history_reader.read_stream_snapshot(
            session_id,
            after_event_sequence=after_event_sequence,
            after_progress_ingest_sequence=after_progress_ingest_sequence,
            after_run_sequence=after_run_sequence,
            active_run_id=active_run_id,
            limit=limit,
            include_latest_if_empty=include_latest_if_empty,
        )
        assistants: dict[str, AssistantPrefix] = {}
        if self._assistant_streams is not None:
            for turn in active.turns:
                if turn.run.status not in _ACTIVE_RUN_STATUSES:
                    continue
                prefix = self._assistant_streams.snapshot(
                    session_id, turn.run.run_id
                )
                if prefix is not None:
                    assistants[turn.run.run_id] = prefix
        boundary_active_run = max(
            (
                turn.run
                for turn in active.turns
                if turn.run.status in _ACTIVE_RUN_STATUSES
            ),
            key=lambda run: (run.sequence, run.run_id),
            default=None,
        )
        token = encode_sync_token(
            session_id=session_id,
            event_sequence=active.snapshot_event_seq,
            progress_ingest_sequence=active.snapshot_progress_ingest_seq,
            run_sequence=active.snapshot_run_seq,
            active_run_id=(
                boundary_active_run.run_id
                if boundary_active_run is not None
                else None
            ),
        )
        return _snapshot_payload(active, assistants), active, assistants, token

    async def list_progress_after(
        self, session_id: str, after_ingest_sequence: int
    ) -> list[ProgressEntry]:
        """读取会话中尚未被客户端同步令牌覆盖的 Progress。"""
        if self._observability is None:
            return []
        return await self._observability.list_progress_after(
            session_id, after_ingest_sequence
        )

def _event_response(value: ApplicationEvent) -> EventResponse:
    """将事件信封转换为 SSE 使用的应用响应。

    参数：
    - `value`（ApplicationEvent）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - EventResponse：供调用方展示或继续处理的 `EventResponse` 响应对象。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return EventResponse(
        event_type=str(value.event_type), durability=str(value.durability), session_id=value.session_id,
        session_seq=value.session_seq, run_id=value.run_id, message_id=value.message_id,
        attachment_id=value.attachment_id, stream_id=value.stream_id, stream_type=value.stream_type,
        chunk_id=value.chunk_id, is_complete=value.is_complete, parent_run_id=value.parent_run_id,
        transition_id=value.transition_id, occurred_at=value.occurred_at, payload=dict(value.payload),
    )


def encode_sync_token(
    *,
    session_id: str,
    event_sequence: int,
    progress_ingest_sequence: int,
    run_sequence: int,
    active_run_id: str | None = None,
) -> str:
    """编码 messages 与 SSE 共享的原子同步水位。"""
    value: dict[str, object] = {
        "s": session_id,
        "e": max(0, event_sequence),
        "p": max(0, progress_ingest_sequence),
        "r": max(0, run_sequence),
    }
    if active_run_id:
        value["a"] = active_run_id
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_sync_token(
    value: str | None,
    *,
    session_id: str,
) -> tuple[int, int, int, str | None]:
    """解析复合同步令牌；无效或不透明格式一律拒绝。"""
    if not value:
        return 0, 0, 0, None
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        parsed = json.loads(raw)
        token_session_id = parsed["s"]
        event_sequence = parsed["e"]
        progress_sequence = parsed["p"]
        run_sequence = parsed["r"]
        active_run_id = parsed.get("a")
        if (
            not isinstance(token_session_id, str)
            or token_session_id != session_id
            or not isinstance(event_sequence, int)
            or isinstance(event_sequence, bool)
            or not isinstance(progress_sequence, int)
            or isinstance(progress_sequence, bool)
            or not isinstance(run_sequence, int)
            or isinstance(run_sequence, bool)
            or event_sequence < 0
            or progress_sequence < 0
            or run_sequence < 0
            or (
                active_run_id is not None
                and (
                    not isinstance(active_run_id, str)
                    or not active_run_id
                )
            )
        ):
            raise ValueError
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError("invalid_sync_token") from exc
    return event_sequence, progress_sequence, run_sequence, active_run_id


def _snapshot_payload(
    active: ActiveSessionSnapshot,
    assistants: Mapping[str, AssistantPrefix],
) -> dict[str, object]:
    """把数据库事实与内存 assistant 前缀投影为聊天快照。"""
    turns = [
        _turn_snapshot_payload(
            turn,
            assistants.get(turn.run.run_id),
        )
        for turn in active.turns
    ]
    latest = turns[-1] if turns else None
    return {
        "run_id": latest["run_id"] if latest is not None else None,
        "run_status": latest["run_status"] if latest is not None else None,
        "turns": turns,
    }


def _turn_snapshot_payload(
    active: SessionTurnSnapshot,
    assistant: AssistantPrefix | None,
) -> dict[str, object]:
    """把一个完整 Run 的数据库事实投影为快照轮次。"""
    run = active.run
    user = next(
        (
            item
            for item in active.messages
            if item.id == run.message_id and item.role.value == "user"
        ),
        None,
    )
    persisted_assistant = next(
        (
            item
            for item in active.messages
            if item.role.value == "assistant" and item.run_id == run.run_id
        ),
        None,
    )
    progress_by_execution: dict[str, list[ProgressEntry]] = defaultdict(list)
    root_process: list[dict[str, str]] = []
    for item in sorted(
        active.progress,
        key=lambda entry: (entry.ingest_sequence,),
    ):
        if item.source is ProgressSource.ROOT:
            root_process.append(
                {
                    "progress_id": item.progress_id,
                    "time": item.created_at.isoformat(),
                    "content": item.content,
                }
            )
        elif item.execution_id:
            progress_by_execution[item.execution_id].append(item)
    tasks: list[dict[str, object]] = []
    for task in active.tasks:
        result_payload: dict[str, str] | None = None
        if task.status in {
            "completed",
            "failed",
            "timed_out",
            "cancelled",
            "superseded",
        }:
            result_time = (
                task.display_result_at or task.completed_at or task.created_at
            )
            result_text = (task.display_result or "").strip() or {
                "completed": "任务已完成。",
                "failed": "任务执行失败。",
                "timed_out": "任务执行失败。",
                "cancelled": "任务已取消。",
                "superseded": "任务已取消。",
            }.get(task.status, "任务未完成。")
            result_payload = {
                "time": result_time.isoformat(),
                "content": result_text,
            }
        tasks.append(
            {
                "task_id": task.task_id,
                "execution_id": task.execution_id,
                "worker_name": task.worker_name,
                "task_title": task.task_title,
                "status": task.status,
                "process": [
                    {
                        "progress_id": item.progress_id,
                        "time": item.created_at.isoformat(),
                        "content": item.content,
                    }
                    for item in sorted(
                        progress_by_execution[task.execution_id],
                        key=lambda entry: (entry.sequence, entry.ingest_sequence),
                    )
                ],
                "result": result_payload,
            }
        )
    # 终态失败或取消不会留下最终 assistant 消息；此时内存中的半截正文
    # 不能伪装成最终回答，也不能继续展示为可恢复的流。
    assistant_payload = None
    if assistant is not None and (
        assistant.completed or run.status not in {"completed", "failed", "cancelled"}
    ):
        assistant_payload = {
            "message_id": f"assistant:{run.run_id}",
            "time": assistant.started_at.isoformat(),
            "content": assistant.content,
            "completed": assistant.completed,
            "last_chunk_id": assistant.last_chunk_id,
        }
    elif persisted_assistant is not None:
        assistant_payload = {
            "message_id": persisted_assistant.id,
            "time": persisted_assistant.timestamp.isoformat(),
            "content": persisted_assistant.content,
            "completed": True,
            "last_chunk_id": 0,
        }
    return {
        "run_id": run.run_id,
        "run_status": run.status,
        "user": (
            {
                "message_id": user.id,
                "time": user.timestamp.isoformat(),
                "content": user.content,
            }
            if user is not None
            else None
        ),
        "root_process": root_process,
        "tasks": tasks,
        "assistant": assistant_payload,
    }
