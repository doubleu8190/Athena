"""个人检索评估工作台 REST API。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from athena.evaluation.portal import EvaluationPortal
from athena.runtime import runtime_from

router = APIRouter(prefix="/evaluation", tags=["evaluation"])


class FeedbackRequest(BaseModel):
    event_id: str
    rating: Literal["accepted", "rejected", "corrected"]
    correct_result_ids: list[str] = Field(default_factory=list)
    expect_empty: bool | None = None
    gain: Literal[1, 2, 3] | None = None
    comment: str | None = None


class PromoteRequest(BaseModel):
    case_id: str | None = None
    labels: list[str] | None = None


class EvaluationSettingsRequest(BaseModel):
    record_enabled: bool | None = None
    record_sample_rate: float | None = Field(default=None, ge=0, le=1)


class ClearRecordsRequest(BaseModel):
    targets: list[Literal["records", "feedback", "cases", "reports"]]


def _portal(request: Request) -> EvaluationPortal:
    portal = runtime_from(request).evaluation_portal
    return portal


def _page(items: list[dict[str, Any]], limit: int, cursor: str | None) -> dict[str, Any]:
    offset = _offset(cursor)
    page = items[offset: offset + limit]
    next_offset = offset + limit if offset + limit < len(items) else None
    return {"items": page, "total": len(items), "next_cursor": str(next_offset) if next_offset is not None else None}


def _offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        value = int(cursor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="cursor must be an integer offset") from exc
    if value < 0:
        raise HTTPException(status_code=400, detail="cursor must be non-negative")
    return value


@router.get("/events")
async def list_events(
    request: Request,
    session_id: str | None = None,
    source: str | None = None,
    feedback_status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
) -> dict[str, Any]:
    return _page(_portal(request).list_events(session_id=session_id, source=source, feedback_status=feedback_status), limit, cursor)


@router.get("/events/{event_id}")
async def get_event(event_id: str, request: Request) -> dict[str, Any]:
    event = _portal(request).get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Retrieval event not found")
    return event


@router.post("/feedback")
async def submit_feedback(req: FeedbackRequest, request: Request) -> dict[str, Any]:
    try:
        return _portal(request).submit_feedback(req.model_dump(exclude_none=True))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/feedback")
async def list_feedback(
    request: Request,
    status: str | None = None,
    rating: str | None = None,
    session_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
) -> dict[str, Any]:
    return _page(_portal(request).list_feedback(status=status, rating=rating, session_id=session_id), limit, cursor)


@router.post("/feedback/{feedback_id}/promote")
async def promote_feedback(feedback_id: str, request: Request, req: PromoteRequest | None = None) -> dict[str, Any]:
    try:
        body = req or PromoteRequest()
        return _portal(request).promote(feedback_id, case_id=body.case_id, labels=body.labels)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/cases")
async def list_cases(
    request: Request,
    label: str | None = None,
    source: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
) -> dict[str, Any]:
    return _page(_portal(request).list_cases(label=label, source=source, status=status), limit, cursor)


@router.get("/reports")
async def list_reports(
    request: Request,
    kind: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
) -> dict[str, Any]:
    return _page(_portal(request).list_reports(kind=kind), limit, cursor)


@router.get("/reports/{report_id}")
async def get_report(report_id: str, request: Request) -> dict[str, Any]:
    report = _portal(request).get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Evaluation report not found")
    return report


@router.get("/settings")
async def get_evaluation_settings(request: Request) -> dict[str, Any]:
    return _portal(request).settings()


@router.patch("/settings")
async def update_evaluation_settings(req: EvaluationSettingsRequest, request: Request) -> dict[str, Any]:
    try:
        return _portal(request).update_settings(record_enabled=req.record_enabled, record_sample_rate=req.record_sample_rate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/records")
async def clear_evaluation_records(req: ClearRecordsRequest, request: Request) -> dict[str, Any]:
    try:
        _portal(request).clear(req.targets)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "cleared", "targets": req.targets}
