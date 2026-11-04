"""协调 Progress 持久化、实时通知和 Trace 写入。"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from uuid import uuid4

from domain.events import ApplicationEvent, EventDurability, EventPort, EventType
from shared import get_logger

from .contracts import (
    LLMSpanAttributes,
    ObservabilityRepository,
    PlanSpanAttributes,
    ProgressAppend,
    ProgressEntry,
    ProgressSource,
    RetrievalSpanAttributes,
    RootNodeSpanAttributes,
    RunSpanAttributes,
    ToolSpanAttributes,
    TraceSpan,
    TraceSpanAttributes,
    WorkerExecutionSpanAttributes,
    WorkerNodeSpanAttributes,
    sanitize_trace_error,
)


class ProgressConflictError(ValueError):
    """同一执行序号被写入了不同 Progress。"""


class ProgressExecutionTerminalError(ValueError):
    """执行已结束后仍收到 Progress。"""


_current_span_id: ContextVar[str | None] = ContextVar(
    "athena_current_span_id", default=None
)
logger = get_logger(__name__)


def current_span_id() -> str | None:
    """返回当前异步上下文关联的 Trace span。"""
    return _current_span_id.get()


def bind_span(span_id: str) -> Token[str | None]:
    """为当前异步执行上下文绑定父 span。"""
    return _current_span_id.set(span_id)


def reset_span(token: Token[str | None]) -> None:
    """恢复绑定前的 span 上下文。"""
    _current_span_id.reset(token)


class RunObservabilityService:
    """为 Root、Worker 和 API 提供同一套 Trace/Progress 能力。"""

    def __init__(
        self,
        repository: ObservabilityRepository,
        events: EventPort,
        *,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._events = events
        self._id_factory = id_factory or (lambda: uuid4().hex)

    async def append_progress(self, value: ProgressAppend) -> ProgressEntry:
        """写入一条不可变 Progress，随后只发送实时通知。"""
        existing = await self._repository.find_progress(
            execution_id=value.execution_id or "",
            sequence=value.sequence,
        ) if value.execution_id else None
        if existing is not None:
            if existing.content == value.content:
                return existing
            raise ProgressConflictError("progress_sequence_conflict")
        entry = await self._repository.append_progress(value)
        await self.publish_progress(entry)
        return entry

    async def publish_progress(self, entry: ProgressEntry) -> None:
        """通知聊天 SSE 重新读取一条已经落库的 Progress。"""
        payload = {
            "progress_id": entry.progress_id,
            "execution_id": entry.execution_id,
            "task_id": entry.task_id,
            "worker_id": entry.worker_id,
            "source": entry.source.value,
            "sequence": entry.sequence,
            "content": entry.content,
            "created_at": entry.created_at.isoformat(),
        }
        if entry.worker_name is not None:
            payload["worker_name"] = entry.worker_name
        if entry.task_title is not None:
            payload["task_title"] = entry.task_title
        await self._events.publish_realtime(
            ApplicationEvent(
                event_type=EventType.PROGRESS_CREATED,
                durability=EventDurability.REALTIME,
                session_id=entry.session_id,
                run_id=entry.run_id,
                occurred_at=entry.created_at,
                payload=payload,
            )
        )

    async def append_root_progress(
        self, *, session_id: str, run_id: str, content: str
    ) -> ProgressEntry:
        """追加 Root 过程；序号由 Athena 在 Run 范围内生成。"""
        entries = await self._repository.list_progress_for_run(run_id)
        root_sequences = [
            item.sequence for item in entries if item.source is ProgressSource.ROOT
        ]
        sequence = max(root_sequences, default=0) + 1
        return await self.append_progress(
            ProgressAppend(
                session_id=session_id,
                run_id=run_id,
                source=ProgressSource.ROOT,
                sequence=sequence,
                content=content.strip(),
            )
        )

    async def start_span(
        self,
        *,
        session_id: str,
        run_id: str,
        name: str,
        attributes: TraceSpanAttributes,
        parent_span_id: str | None = None,
    ) -> TraceSpan:
        """创建 running span。"""
        return await self._repository.start_span(
            span_id=self._id_factory(),
            session_id=session_id,
            run_id=run_id,
            parent_span_id=parent_span_id or current_span_id(),
            name=name,
            started_at=datetime.now(timezone.utc),
            attributes=attributes,
        )

    async def finish_span(
        self,
        span_id: str,
        *,
        status: str = "completed",
        error: dict[str, str] | None = None,
    ) -> None:
        """结束 span；追踪写入失败不覆盖业务错误。"""
        try:
            await self._repository.finish_span(
                span_id=span_id,
                status=status,
                completed_at=datetime.now(timezone.utc),
                duration_ms=0,
                error=sanitize_trace_error(error),
            )
        except Exception:
            # Trace 是诊断事实，收尾失败不能替换节点已经产生的业务异常。
            logger.warning(
                "trace_span_finish_failed",
                span_id=span_id,
                exc_info=True,
            )

    async def list_run_spans(self, run_id: str) -> list[TraceSpan]:
        """读取一个 Run 的完整 Trace。"""
        return await self._repository.list_spans_for_run(run_id)

    async def list_progress_after(
        self, session_id: str, after_ingest_sequence: int
    ) -> list[ProgressEntry]:
        """读取会话中位于 Progress 水位之后的不可变条目。"""
        return await self._repository.list_progress_after(
            session_id, after_ingest_sequence
        )

    async def progress_ingest_watermark(self) -> int:
        """返回全局 Progress 写入水位，供同步令牌和 SSE 基线使用。"""
        return await self._repository.progress_ingest_watermark()


__all__ = [
    "LLMSpanAttributes",
    "PlanSpanAttributes",
    "ProgressConflictError",
    "ProgressExecutionTerminalError",
    "RunObservabilityService",
    "RunSpanAttributes",
    "RetrievalSpanAttributes",
    "RootNodeSpanAttributes",
    "ToolSpanAttributes",
    "WorkerExecutionSpanAttributes",
    "WorkerNodeSpanAttributes",
    "bind_span",
    "current_span_id",
    "reset_span",
]
