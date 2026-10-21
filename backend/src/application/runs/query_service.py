"""运行列表查询用例。"""

from __future__ import annotations

from domain.events import EventHistoryPort
from domain.runs import RunQueryPort, RunStatus, RunSummary
from domain.sessions import SessionRepository

from application.common.responses import RunSummaryResponse, RunTimelineEventResponse, RunTimelineResponse

from application.sessions import SessionNotFoundError
from .command_service import RunNotFoundError


class RunQueryService:
    """把会话存在性校验和运行查询组合起来。"""

    def __init__(self, sessions: SessionRepository, runs: RunQueryPort, events: EventHistoryPort | None = None) -> None:
        self._sessions = sessions
        self._runs = runs
        self._events = events

    async def list_for_session(self, session_id: str) -> list[RunSummaryResponse]:
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)
        return [_to_response(value) for value in await self._runs.list_for_session(session_id)]

    async def get(self, run_id: str) -> RunSummaryResponse:
        value = await self._runs.get(run_id)
        if value is None:
            raise RunNotFoundError(run_id)
        return _to_response(value)

    async def timeline(self, session_id: str, run_id: str, *, after: int = 0, limit: int = 200) -> RunTimelineResponse:
        if await self._sessions.get(session_id) is None:
            raise SessionNotFoundError(session_id)
        run = next((item for item in await self._runs.list_for_session(session_id) if item.run_id == run_id), None)
        if run is None:
            raise RunNotFoundError(run_id)
        cursor = max(0, after)
        if self._events is None:
            return RunTimelineResponse(run=_to_timeline_run_response(run), events=(), next_after_seq=cursor, has_more=False)
        values, has_more = await self._events.list_events_for_run(
            session_id, run_id, after=cursor, limit=min(max(limit, 1), 500)
        )
        events = tuple(_timeline_event(value) for value in values if value.session_seq is not None)
        next_after = events[-1].session_seq if events else cursor
        retrieval_ids = tuple(dict.fromkeys(
            str(event.payload["retrieval_run_id"])
            for event in events
            if event.payload.get("retrieval_run_id")
        ))
        plan_ids = tuple(dict.fromkeys(str(event.payload["plan_id"]) for event in events if event.payload.get("plan_id")))
        task_ids = tuple(dict.fromkeys(str(event.payload["task_id"]) for event in events if event.payload.get("task_id")))
        return RunTimelineResponse(
            run=_to_timeline_run_response(run), events=events, next_after_seq=next_after, has_more=has_more,
            links={"retrieval_run_ids": retrieval_ids, "plan_ids": plan_ids, "task_ids": task_ids},
        )


def _to_response(value: RunSummary) -> RunSummaryResponse:
    """将运行摘要实体转换为应用层响应。"""
    return RunSummaryResponse(
        run_id=value.run_id,
        session_id=value.session_id,
        status=value.status.value if isinstance(value.status, RunStatus) else str(value.status),
        root_thread_id=value.root_thread_id,
        error=value.error,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _to_timeline_run_response(value: RunSummary) -> RunSummaryResponse:
    response = _to_response(value)
    error = response.error
    if not isinstance(error, dict):
        safe_error = None
    else:
        safe_error = {
            key: _safe_text(item) if key == "message" else item
            for key, item in error.items()
            if key in {"code", "message", "retryable", "stage"}
        }
    return RunSummaryResponse(
        run_id=response.run_id,
        session_id=response.session_id,
        status=response.status,
        root_thread_id=response.root_thread_id,
        error=safe_error,
        created_at=response.created_at,
        updated_at=response.updated_at,
    )


_TIMELINE_FIELDS = {
    "activity_id", "activity_type", "name", "node", "status", "summary", "duration_ms",
    "started_at", "completed_at", "parent_activity_id", "parent_run_id", "plan_id", "task_id",
    "execution_generation", "worker_run_id", "tool_call_id", "call_id", "retrieval_run_id",
    "provider", "model", "attempt", "input_tokens", "output_tokens", "candidate_count", "result_count", "selected_count", "injected_count", "retrieval_run_id",
    "decision", "tool_name", "title", "stage", "retryable", "code", "error",
    "approval_id", "risk_level",
}


def _safe_text(value: object, limit: int = 240) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _safe_payload(payload: dict[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key, value in payload.items():
        if key not in _TIMELINE_FIELDS:
            continue
        if key == "error" and isinstance(value, dict):
            safe["error"] = {
                nested_key: (_safe_text(nested_value) if nested_key == "message" else nested_value)
                for nested_key, nested_value in value.items()
                if nested_key in {"code", "message", "retryable", "stage"}
            }
        elif isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = _safe_text(value) if isinstance(value, str) else value
    if "error" not in safe and isinstance(payload.get("error_detail"), dict):
        error = payload["error_detail"]
        safe["error"] = {
            nested_key: (_safe_text(nested_value) if nested_key == "message" else nested_value)
            for nested_key, nested_value in error.items()
            if nested_key in {"code", "message", "retryable", "stage"}
        }
    return safe


def _timeline_event(value: object) -> RunTimelineEventResponse:
    event = value
    return RunTimelineEventResponse(
        session_seq=int(event.session_seq),
        event_type=str(event.event_type),
        occurred_at=event.occurred_at,
        run_id=event.run_id,
        payload=_safe_payload(dict(event.payload)),
    )

__all__ = ["RunNotFoundError", "RunQueryService"]
