"""Session 下属消息查询用例。"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from typing import Any

from domain.events import ApplicationEvent
from domain.sessions import SessionRepository

from application.common.responses import (
    HistoryContentResponse,
    HistoryTaskResponse,
    HistoryTurnResponse,
    SessionMessageHistoryResponse,
)
from .history_port import SessionHistoryQueryPort

from .service import SessionNotFoundError

class SessionMessageQueryService:
    """将已结束用户消息、执行事件和结果组装成分页历史。"""

    def __init__(
        self,
        sessions: SessionRepository,
        history_reader: SessionHistoryQueryPort,
    ) -> None:
        """保存查询所需的仓储依赖。

        参数：
        - `sessions`（SessionRepository）：用于确认会话存在。
        - `history_reader`（SessionHistoryQueryPort）：在单一一致性快照中读取 Run、消息和事件。

        返回：
        - None：依赖初始化完成。

        异常：
        - 无预期业务异常。
        """
        self._sessions = sessions
        self._history_reader = history_reader

    async def list_messages(
        self,
        session_id: str,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> SessionMessageHistoryResponse:
        """读取已结束的用户消息轮次，并保持轮次分页原子性。

        参数：
        - `session_id`（str）：目标会话标识。
        - `limit`（int）：本页轮次数，范围限制为 1 到 100。
        - `cursor`（str | None）：上一页最早 Run 序号，用于加载更早轮次。

        返回：
        - SessionMessageHistoryResponse：按时间正序排列的完整历史轮次及下一页游标。

        异常：
        - SessionNotFoundError：会话不存在。
        - ValueError：游标不是正整数。
        """
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)
        before_sequence = _parse_cursor(cursor, session_id)
        page_limit = min(max(limit, 1), 100)
        snapshot_events: dict[str, list[ApplicationEvent]] = {}
        snapshot = await self._history_reader.read_snapshot(
            session_id,
            before_sequence=before_sequence,
            limit=page_limit,
        )
        selected = list(snapshot.runs)
        messages = list(snapshot.messages)
        has_more = snapshot.has_more
        for event in snapshot.events:
            if event.run_id:
                snapshot_events.setdefault(event.run_id, []).append(event)
        message_by_id = {message.id: message for message in messages}
        turns: list[HistoryTurnResponse] = []
        for run in selected:
            user = message_by_id.get(run.message_id)
            if user is None or user.role.value != "user":
                continue
            assistant = next(
                (item for item in messages if item.role.value == "assistant" and item.run_id == run.run_id),
                None,
            )
            event_values = snapshot_events.get(run.run_id, [])
            root_process, tasks = _project_events(event_values)
            turns.append(HistoryTurnResponse(
                run_id=run.run_id,
                user=HistoryContentResponse(user.timestamp, user.content, user.id),
                root_process=tuple(root_process),
                tasks=tuple(tasks),
                assistant=(HistoryContentResponse(assistant.timestamp, assistant.content) if assistant else None),
            ))

        next_cursor = _encode_cursor(session_id, selected[-1].sequence) if has_more and selected else None
        turns.sort(key=lambda turn: (turn.user.time, turn.run_id))
        return SessionMessageHistoryResponse(
            tuple(turns),
            next_cursor,
            snapshot.snapshot_event_seq,
        )

def _parse_cursor(cursor: str | None, session_id: str) -> int | None:
    """解析绑定会话的历史游标。"""
    if cursor is None:
        return None
    try:
        padded_cursor = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(padded_cursor.encode("ascii")).decode("utf-8")
        cursor_session_id, raw_sequence = decoded.rsplit(":", 1)
        value = int(raw_sequence)
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("invalid_history_cursor") from exc
    if cursor_session_id != session_id or value < 1:
        raise ValueError("invalid_history_cursor")
    return value


def _encode_cursor(session_id: str, sequence: int) -> str:
    """生成绑定会话与 Run 序号的不透明游标。"""
    value = f"{session_id}:{sequence}".encode("utf-8")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _project_events(events: list[ApplicationEvent]) -> tuple[list[HistoryContentResponse], list[HistoryTaskResponse]]:
    """将白名单事件归并为 Root 过程和按 execution_id 分组的 Worker 执行。"""
    execution_by_task_generation = _execution_indexes(events)
    root: list[HistoryContentResponse] = []
    task_process: dict[str, list[HistoryContentResponse]] = {}
    task_started: dict[str, datetime] = {}
    task_results: dict[str, str | None] = {}
    task_result_times: dict[str, datetime] = {}
    task_result_sequences: dict[str, int | None] = {}
    task_order: dict[str, int] = {}
    for event in events:
        event_type = str(event.event_type)
        payload = dict(event.payload)
        occurred_at = event.occurred_at
        execution_id = _worker_execution_id(
            event_type,
            payload,
            execution_by_task_generation,
        )
        content = _event_content(event_type, payload)
        if content is None:
            continue
        entry = HistoryContentResponse(
            occurred_at,
            content,
            session_seq=event.session_seq,
        )
        if execution_id and event_type.startswith(("task.", "tool.", "approval.", "llm.")):
            task_process.setdefault(execution_id, []).append(entry)
            task_order.setdefault(execution_id, int(event.session_seq or 0))
            task_started.setdefault(execution_id, occurred_at)
            if event_type == "task.started":
                task_started[execution_id] = occurred_at
            if event_type in {"task.completed", "task.failed", "task.cancelled"}:
                task_results[execution_id] = _result_text(payload)
                task_result_times[execution_id] = _completed_at(payload, occurred_at)
                task_result_sequences[execution_id] = event.session_seq
        else:
            root.append(entry)
    tasks = [
        HistoryTaskResponse(
            time=task_result_times.get(execution_id, task_started[execution_id]),
            process=tuple(task_process[execution_id]),
            content=task_results.get(execution_id),
            session_seq=task_result_sequences.get(execution_id),
        )
        for execution_id in sorted(
            task_process,
            key=lambda key: task_order[key],
        )
    ]
    return root, tasks


def _execution_indexes(
    events: list[ApplicationEvent],
) -> dict[tuple[str, int], str]:
    """建立 Task 代次到 Execution 的索引，供没有 execution_id 的 Worker 事件归组。"""
    indexes: dict[tuple[str, int], str] = {}
    for event in events:
        payload = dict(event.payload)
        execution_id = _text(payload.get("execution_id"))
        task_id = _text(payload.get("task_id"))
        generation = _integer(payload.get("execution_generation"))
        if generation is None:
            generation = _integer(payload.get("attempt"))
        if execution_id and task_id and generation is not None:
            indexes.setdefault((task_id, generation), execution_id)
    return indexes


def _worker_execution_id(
    event_type: str,
    payload: dict[str, Any],
    execution_indexes: dict[tuple[str, int], str],
) -> str:
    """读取显式 Execution 身份，或从 Worker LLM 的父活动身份恢复。"""
    explicit = _text(payload.get("execution_id"))
    if explicit:
        return explicit
    if not event_type.startswith("llm."):
        return ""
    parts = _text(payload.get("parent_activity_id")).split(":")
    if len(parts) != 4 or parts[0] != "task" or parts[2] != "exec":
        return ""
    generation = _integer(parts[3])
    if generation is None:
        return ""
    return execution_indexes.get((parts[1], generation), "")


def _event_content(event_type: str, payload: dict[str, Any]) -> str | None:
    """从明确支持的事件类型生成简短历史文案。"""
    error_value = payload.get("error")
    error = _text(error_value.get("message") if isinstance(error_value, dict) else error_value)
    task_title = _text(payload.get("title") or payload.get("objective")) or "Worker 任务"
    labels = {
        "plan.accepted": "执行计划已受理",
        "plan.created": "已制定执行计划",
        "plan.revised": "执行计划已调整",
        "plan.cancelling": "正在停止执行计划",
        "plan.completed": "执行计划已完成",
        "plan.failed": "执行计划未能完成",
        "plan.cancelled": "执行计划已取消",
        "round.started": "开始执行一轮任务",
        "round.completed": "本轮任务已处理完毕",
        "synthesis.started": "正在汇总处理结果",
        "synthesis.completed": "处理结果已汇总",
        "run.queued": "任务已排队",
        "run.started": "任务开始执行",
        "run.resumed": "任务继续执行",
        "run.stopping": "正在停止任务",
        "run.completed": "任务已完成",
        "run.cancelled": "任务已取消",
        "run.failed": "任务未能完成",
        "run.budget_exceeded": "任务已达到运行限制",
        "run.root_suspended": "主任务等待子任务继续",
        "task.queued": f"任务已加入执行计划：{task_title}",
        "task.started": f"任务已开始：{task_title}",
        "task.retrying": f"任务准备重试：{task_title}",
        "task.cancelling": f"正在停止任务：{task_title}",
        "task.waiting_approval": f"任务等待确认：{task_title}",
        "task.completed": f"任务执行成功：{task_title}",
        "task.failed": f"任务执行失败：{task_title}",
        "task.cancelled": f"任务已取消：{task_title}",
        "approval.required": "需要确认操作",
        "approval.resolved": "操作确认已处理",
        "tool.started": f"开始调用工具：{_text(payload.get('tool_name')) or '工具'}",
        "tool.completed": f"工具调用结束：{_text(payload.get('tool_name')) or '工具'}",
        "llm.started": "正在调用模型",
        "llm.completed": "模型调用完成",
        "llm.validation_failed": "模型输出校验失败",
        "agent.waiting_for_files": "等待文件处理完成后继续",
        "attachment_updated": "附件状态已更新",
        "file_processing_started": "正在处理文件",
        "file_processing_completed": "文件已处理完成",
        "file_processing_failed": "文件处理失败",
    }
    if event_type in {"node.started", "node.completed", "node.failed"}:
        node = _text(payload.get("node") or payload.get("name"))
        if not node:
            return None
        node_status = {
            "node.started": "开始处理",
            "node.completed": "处理完成",
            "node.failed": "处理失败",
        }[event_type]
        content = f"{node_status}：{node}"
    elif event_type in {"retrieval.started", "retrieval.completed", "retrieval.failed"}:
        content = "正在检索相关资料" if event_type.endswith("started") else "资料检索失败" if event_type.endswith("failed") else "相关资料检索完成"
    elif event_type.startswith("retrieval.provider."):
        provider = _text(payload.get("provider") or payload.get("name")) or "检索来源"
        status = str(payload.get("status") or event_type.rsplit(".", 1)[-1])
        content = f"{provider}检索中" if status == "started" or status == "running" else f"{provider}检索失败" if status in {"failed", "timeout"} else f"{provider}检索完成"
    elif event_type == "retrieval.selection.completed":
        content = f"已选择 {payload.get('context_count', 0)} 条资料用于回答"
    else:
        content = labels.get(event_type)
    if content is None:
        return None
    if error and event_type.endswith(("failed", "retrying")):
        content = f"{content}：{error[:240]}"
    if (
        event_type == "llm.completed"
        and _text(payload.get("status")) == "failed"
    ):
        content = "模型调用失败"
    if event_type == "tool.completed" and str(payload.get("status", "success")) != "success":
        content = f"工具调用失败：{_text(payload.get('tool_name')) or '工具'}"
    return content


def _result_text(payload: dict[str, Any]) -> str | None:
    """只返回持久事件明确提供的可展示结果摘要。"""
    summary = payload.get("result_summary")
    if not isinstance(summary, str):
        return None
    return summary[:12000] or None


def _text(value: object) -> str:
    """仅接受文本值，避免把任意事件结构直接序列化展示。"""
    return value if isinstance(value, str) else ""


def _integer(value: object) -> int | None:
    """只接受可转换的非负整数值。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _completed_at(payload: dict[str, Any], fallback: datetime) -> datetime:
    """优先使用事件声明的结束时间，并在缺失或无效时使用事件持久化时间。"""
    value = payload.get("completed_at")
    if not isinstance(value, str) or not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
