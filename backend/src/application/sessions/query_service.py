"""Session 下属消息查询用例。"""

from __future__ import annotations

import base64
import binascii
import json
from collections import defaultdict

from application.common.responses import (
    HistoryContentResponse,
    HistoryTaskResponse,
    HistoryTurnResponse,
    SessionMessageHistoryResponse,
)
from application.observability import ProgressEntry, ProgressSource
from domain.sessions import SessionRepository

from .history_port import HistoryTaskRecord, SessionHistoryQueryPort
from .service import SessionNotFoundError


class SessionMessageQueryService:
    """将终态 Run、Progress、Worker 展示结果和最终回答组装为历史轮次。"""

    def __init__(
        self,
        sessions: SessionRepository,
        history_reader: SessionHistoryQueryPort,
    ) -> None:
        self._sessions = sessions
        self._history_reader = history_reader

    async def list_messages(
        self,
        session_id: str,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> SessionMessageHistoryResponse:
        """读取终态用户消息轮次，并按用户消息保证整页原子性。

        参数：
        - `session_id`：目标会话。
        - `limit`：本页用户消息数，限制为 1–100。
        - `cursor`：绑定会话的上一页最早 Run 序号。

        返回：
        - 仅含展示字段的历史 items、下一页游标和 SSE 同步令牌。

        异常：
        - SessionNotFoundError：会话不存在。
        - ValueError：分页游标无效。
        """
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)
        before_sequence = _parse_cursor(cursor, session_id)
        snapshot = await self._history_reader.read_snapshot(
            session_id,
            before_sequence=before_sequence,
            limit=min(max(limit, 1), 100),
        )
        messages_by_id = {message.id: message for message in snapshot.messages}
        progress_by_run = defaultdict(list)
        for item in snapshot.progress:
            progress_by_run[item.run_id].append(item)
        tasks_by_run = defaultdict(list)
        for item in snapshot.tasks:
            tasks_by_run[item.run_id].append(item)

        turns: list[HistoryTurnResponse] = []
        for run in snapshot.runs:
            user = messages_by_id.get(run.message_id)
            if user is None or user.role.value != "user":
                continue
            assistant = next(
                (
                    item
                    for item in snapshot.messages
                    if item.role.value == "assistant" and item.run_id == run.run_id
                ),
                None,
            )
            entries = progress_by_run[run.run_id]
            root_process = tuple(
                HistoryContentResponse(time=item.created_at, content=item.content)
                for item in sorted(
                    entries,
                    key=lambda entry: (entry.sequence, entry.ingest_sequence),
                )
                if item.source is ProgressSource.ROOT
            )
            tasks = tuple(
                _task_response(task, entries)
                for task in tasks_by_run[run.run_id]
            )
            turns.append(
                HistoryTurnResponse(
                    user=HistoryContentResponse(
                        time=user.timestamp, content=user.content
                    ),
                    root_process=root_process,
                    tasks=tasks,
                    assistant=(
                        HistoryContentResponse(
                            time=assistant.timestamp,
                            content=assistant.content,
                        )
                        if assistant is not None
                        else None
                    ),
                )
            )

        next_cursor = (
            _encode_cursor(session_id, snapshot.runs[-1].sequence)
            if snapshot.has_more and snapshot.runs
            else None
        )
        turns.sort(key=lambda item: item.user.time)
        return SessionMessageHistoryResponse(
            items=tuple(turns),
            next_cursor=next_cursor,
            sync_token=_encode_sync_token(
                session_id=session_id,
                event_sequence=snapshot.snapshot_event_seq,
                progress_ingest_sequence=snapshot.snapshot_progress_ingest_seq,
                run_sequence=snapshot.snapshot_run_seq,
                active_run_id=snapshot.active_run_id,
            ),
        )


def _task_response(
    task: HistoryTaskRecord, entries: list[ProgressEntry]
) -> HistoryTaskResponse:
    """将一个 Worker execution 投影为独立原子任务组。"""
    process = tuple(
        HistoryContentResponse(time=item.created_at, content=item.content)
        for item in sorted(
            entries,
            key=lambda entry: (entry.sequence, entry.ingest_sequence),
        )
        if item.execution_id == task.execution_id
    )
    result_time = task.display_result_at or task.completed_at or task.created_at
    result_text = (task.display_result or "").strip()
    if not result_text:
        result_text = {
            "completed": "任务已完成。",
            "failed": "任务执行失败。",
            "timed_out": "任务执行失败。",
            "cancelled": "任务已取消。",
            "superseded": "任务已取消。",
        }.get(task.status, "任务未完成。")
    return HistoryTaskResponse(
        status=task.status,
        worker_name=task.worker_name,
        task_title=task.task_title,
        process=process,
        result=HistoryContentResponse(time=result_time, content=result_text),
    )


def _parse_cursor(cursor: str | None, session_id: str) -> int | None:
    """解析绑定会话的 user-message 分页游标。"""
    if cursor is None:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        cursor_session_id, raw_sequence = decoded.rsplit(":", 1)
        value = int(raw_sequence)
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("invalid_history_cursor") from exc
    if cursor_session_id != session_id or value < 1:
        raise ValueError("invalid_history_cursor")
    return value


def _encode_cursor(session_id: str, sequence: int) -> str:
    """生成绑定会话与 Run 序号的不透明分页游标。"""
    value = f"{session_id}:{sequence}".encode("utf-8")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _encode_sync_token(
    *,
    session_id: str,
    event_sequence: int,
    progress_ingest_sequence: int,
    run_sequence: int,
    active_run_id: str | None,
) -> str:
    """生成 messages 与 SSE 共享的复合游标；后端是唯一解释方。"""
    value: dict[str, object] = {
        "s": session_id,
        "e": max(0, event_sequence),
        "p": max(0, progress_ingest_sequence),
        "r": max(0, run_sequence),
    }
    if active_run_id:
        value["a"] = active_run_id
    encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


__all__ = ["SessionMessageQueryService"]
