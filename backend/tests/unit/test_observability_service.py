"""Trace span 类型、上下文父子关系和 Root Progress 序号测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from application.observability import (
    LLMSpanAttributes,
    ProgressEntry,
    ProgressSource,
    RootNodeSpanAttributes,
    RunObservabilityService,
    RunSpanAttributes,
    TraceSpan,
    TraceSpanAttributes,
    bind_span,
    reset_span,
    sanitize_trace_error,
)
from domain.events import EventPort
from infrastructure.persistence.postgres.models import (
    Base,
    OrchestratorExecutionModel,
    WorkerDisplayResultModel,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_worker_display_result_uses_independent_create_all_table() -> None:
    assert WorkerDisplayResultModel.__table__ in Base.metadata.tables.values()
    assert {
        column.name for column in WorkerDisplayResultModel.__table__.columns
    } == {"execution_id", "display_result", "display_result_at"}
    assert {
        column.name for column in OrchestratorExecutionModel.__table__.columns
    }.isdisjoint({"display_result", "display_result_at"})


class Events(EventPort):
    def __init__(self):
        self.items = []

    async def publish(self, event):
        self.items.append(event)
        return event

    async def publish_realtime(self, event):
        self.items.append(event)
        return event

    async def open_subscription(self, session_id):
        raise NotImplementedError

    async def list_events_between(self, session_id, after=0, upto=None, limit=256):
        return []

    async def list_events_for_run(
        self, session_id, run_id, *, after=0, limit=200
    ):
        return [], False

    async def close_subscription(self, session_id, queue):
        return None


class Repository:
    def __init__(self):
        self.progress = []
        self.spans = {}

    async def list_progress_for_run(self, run_id):
        return [
            value for value in self.progress if value.run_id == run_id
        ]

    async def append_progress(self, value):
        from application.observability import ProgressEntry

        entry = ProgressEntry(
            **value.model_dump(),
            progress_id=f"progress-{len(self.progress) + 1}",
            created_at=NOW,
            ingest_sequence=len(self.progress) + 1,
        )
        self.progress.append(entry)
        return entry

    async def start_span(self, **values):
        span = TraceSpan(
            **values,
            span_type=values["attributes"].span_type,
            status="running",
            sequence=len(self.spans) + 1,
        )
        self.spans[span.span_id] = span
        return span

    async def finish_span(
        self,
        *,
        span_id,
        status,
        completed_at,
        duration_ms,
        error=None,
    ):
        current = self.spans[span_id]
        self.spans[span_id] = current.model_copy(
            update={
                "status": status,
                "completed_at": completed_at,
                "duration_ms": duration_ms,
                "error": error,
            }
        )


class FinishFailingRepository(Repository):
    async def finish_span(self, **values):
        raise RuntimeError("trace storage unavailable")


@pytest.mark.asyncio
async def test_root_progress_sequence_is_monotonic_and_realtime_only() -> None:
    repository = Repository()
    events = Events()
    service = RunObservabilityService(repository, events)

    first = await service.append_root_progress(
        session_id="session-1",
        run_id="run-1",
        content="正在理解问题",
    )
    second = await service.append_root_progress(
        session_id="session-1",
        run_id="run-1",
        content="正在整理答案",
    )

    assert (first.sequence, second.sequence) == (1, 2)
    assert first.source is ProgressSource.ROOT
    assert [event.event_type for event in events.items] == [
        "progress.created",
        "progress.created",
    ]


@pytest.mark.asyncio
async def test_realtime_progress_payload_includes_display_metadata() -> None:
    """实时进度通知应携带可读 Worker 名称和任务标题。"""
    events = Events()
    service = RunObservabilityService(Repository(), events)
    entry = ProgressEntry(
        progress_id="progress-1",
        session_id="session-1",
        run_id="run-1",
        execution_id="execution-1",
        task_id="task-1",
        worker_id="worker-instance-1",
        source=ProgressSource.WORKER,
        sequence=1,
        content="正在读取资料",
        created_at=NOW,
        ingest_sequence=1,
        worker_name="Worker A",
        task_title="整理资料",
    )

    await service.publish_progress(entry)

    payload = events.items[0].payload
    assert payload["worker_name"] == "Worker A"
    assert payload["task_title"] == "整理资料"
    assert payload["worker_id"] == "worker-instance-1"


@pytest.mark.asyncio
async def test_child_span_inherits_current_node_span() -> None:
    repository = Repository()
    service = RunObservabilityService(repository, Events())
    run_span = await service.start_span(
        session_id="session-1",
        run_id="run-1",
        name="run.execute",
        attributes=RunSpanAttributes(),
    )
    node = await service.start_span(
        session_id="session-1",
        run_id="run-1",
        name="decide_root_action",
        attributes=RootNodeSpanAttributes(node_name="decide_root_action"),
        parent_span_id=run_span.span_id,
    )
    token = bind_span(node.span_id)
    try:
        llm = await service.start_span(
            session_id="session-1",
            run_id="run-1",
            name="llm.generate",
            attributes=LLMSpanAttributes(attempt=1),
        )
    finally:
        reset_span(token)

    assert llm.parent_span_id == node.span_id
    assert llm.attributes.span_type == "llm"


@pytest.mark.asyncio
async def test_trace_finish_failure_does_not_replace_business_result() -> None:
    service = RunObservabilityService(FinishFailingRepository(), Events())

    await service.finish_span("span-1", status="failed", error={"code": "failed"})


def test_trace_attributes_reject_arbitrary_dicts() -> None:
    adapter = TypeAdapter(TraceSpanAttributes)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "span_type": "llm",
                "attempt": 1,
                "prompt": "不得保存",
            }
        )


def test_trace_error_keeps_only_bounded_whitelist_fields() -> None:
    secret = "Authorization: Bearer secret /private/path"

    assert sanitize_trace_error(
        {
            "code": "WORKER_FAILED",
            "type": "RuntimeError",
            "status": "failed",
            "retryable": True,
            "message": secret,
            "traceback": secret,
        }
    ) == {
        "code": "WORKER_FAILED",
        "type": "RuntimeError",
        "status": "failed",
        "retryable": "true",
    }
    span = TraceSpan(
        span_id="span-1",
        session_id="session-1",
        run_id="run-1",
        span_type="run",
        name="run.execute",
        status="failed",
        started_at=NOW,
        attributes=RunSpanAttributes(),
        error={"code": "run_failed", "message": secret},
        sequence=1,
    )
    assert span.error == {"code": "run_failed"}
    assert secret not in span.model_dump_json()
