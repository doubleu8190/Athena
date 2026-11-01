"""Root 浏览器验收用的隔离 fixture server。

提供 canonical 两轮编排快照、重试 Execution、批量审批和有状态 decision。
不连接 PostgreSQL、不调用真实 LLM，因此不代表生产全链路。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from application.common.responses import AuthSessionResponse
from interfaces.http.dto.orchestration import RunOrchestrationSnapshotResponse
from interfaces.http.dto.runs import RunListResponse, RunTimelineResponse
from interfaces.http.dto.sessions import RunSummaryResponse, SessionResponse
from interfaces.http.controllers.query_controller import ProjectResponse

NOW = datetime.now(UTC).replace(microsecond=0)
RUN_ID = "s8-fixture-run"
SESSION_ID = "s8-fixture-session"
PROJECT_ID = "s8-fixture-project"
REQUEST_ID = "s8-fixture-approval-request"
BATCH_ID = "s8-fixture-approval-batch"
ITEMS = ("s8-fixture-item-export", "s8-fixture-item-notify")
app = FastAPI(title="Athena S8 UI fixture", version="fixture")
_item_decisions: dict[str, str] = {}


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str


def _execution(execution_id: str, generation: int, status: str, offset: int, *, reserved: bool = False, error: dict | None = None) -> dict:
    return {"execution_id": execution_id, "generation": generation, "status": status, "started_at": NOW + timedelta(seconds=offset), "deadline_at": NOW + timedelta(seconds=offset + 30), "completed_at": NOW + timedelta(seconds=offset + 1) if status in {"completed", "failed"} else None, "cancellation_epoch": 0, "capacity_reserved": reserved, "error": error}


def _snapshot() -> dict[str, object]:
    resolved = len(_item_decisions) == len(ITEMS)
    denied = any(value == "denied" for value in _item_decisions.values())
    task_status = "failed" if resolved and denied else ("completed" if resolved else "waiting_approval")
    plan_status = "completed" if resolved else "running"
    run_status = "failed" if resolved and denied else ("completed" if resolved else "running")
    request_status = "resolved" if resolved else "pending"
    pending = [{
        "approval_request_id": REQUEST_ID, "pause_id": "s8-fixture-pause", "approval_batch_id": BATCH_ID,
        "run_id": RUN_ID, "plan_id": "s8-fixture-plan-2", "task_id": "s8-fixture-task-approval", "execution_id": "s8-fixture-execution-approval", "generation": 1, "status": request_status, "expires_at": NOW + timedelta(seconds=120), "received_at": NOW + timedelta(seconds=3),
        "items": [
            {"item_id": ITEMS[0], "approval_id": "s8-fixture-approval-export", "tool_name": "fixture.export", "summary": "导出汇总文件", "risk_level": "high", "arguments": {"format": "json"}, "arguments_sha256": "fixture-export-sha256", "status": "resolved" if ITEMS[0] in _item_decisions else "pending", "decision": _item_decisions.get(ITEMS[0]), "created_at": NOW + timedelta(seconds=3), "decided_at": NOW + timedelta(seconds=4) if ITEMS[0] in _item_decisions else None},
            {"item_id": ITEMS[1], "approval_id": "s8-fixture-approval-notify", "tool_name": "fixture.notify", "summary": "通知协作者", "risk_level": "medium", "arguments": {"channel": "internal"}, "arguments_sha256": "fixture-notify-sha256", "status": "resolved" if ITEMS[1] in _item_decisions else "pending", "decision": _item_decisions.get(ITEMS[1]), "created_at": NOW + timedelta(seconds=3), "decided_at": NOW + timedelta(seconds=4) if ITEMS[1] in _item_decisions else None},
        ],
    }]
    payload = {
        "run": {"run_id": RUN_ID, "session_id": SESSION_ID, "status": run_status, "root_thread_id": "s8-fixture-root", "cancellation_epoch": 0, "cancellation_external_effect_status": "none", "created_at": NOW, "updated_at": NOW},
        "worker_plans": [
            {"plan_id": "s8-fixture-plan-1", "run_id": RUN_ID, "sequence": 1, "goal": "第一轮准备并验证输入", "status": "completed", "expected_task_count": 1, "terminal_task_count": 1, "root_wait_generation": 1, "created_at": NOW, "completed_at": NOW + timedelta(seconds=2), "tasks": [{"task_id": "s8-fixture-task-retry", "operation_id": "s8-fixture-operation-retry", "task_ref": "prepare", "objective": "准备输入并回显", "acceptance_criteria": "最终 echo 等于输入", "status": "completed", "active_execution_id": None, "generation": 2, "output": {"echo": {"value": "round-1"}}, "error": None, "executions": [_execution("s8-fixture-execution-1", 1, "failed", 0, error={"code": "TEMPORARY_UNAVAILABLE", "retryable": True}), _execution("s8-fixture-execution-2", 2, "completed", 1)]}]},
            {"plan_id": "s8-fixture-plan-2", "run_id": RUN_ID, "sequence": 2, "goal": "第二轮执行并等待批量审批", "status": plan_status, "expected_task_count": 2, "terminal_task_count": 2 if resolved else 1, "root_wait_generation": 2, "created_at": NOW + timedelta(seconds=2), "completed_at": NOW + timedelta(seconds=5) if resolved else None, "tasks": [
                {"task_id": "s8-fixture-task-review", "operation_id": "s8-fixture-operation-review", "task_ref": "review", "objective": "复核第一轮结果", "acceptance_criteria": "输出复核结论", "status": "completed", "active_execution_id": None, "generation": 1, "output": {"review": "ok"}, "error": None, "executions": [_execution("s8-fixture-execution-review", 1, "completed", 2)]},
                {"task_id": "s8-fixture-task-approval", "operation_id": "s8-fixture-operation-approval", "task_ref": "approval", "objective": "执行需要人工授权的动作", "acceptance_criteria": "审批后继续并返回结果", "status": task_status, "active_execution_id": None if resolved else "s8-fixture-execution-approval", "generation": 1, "output": {"decisions": dict(_item_decisions)} if resolved and not denied else None, "error": {"code": "APPROVAL_DENIED"} if denied else None, "executions": [_execution("s8-fixture-execution-approval", 1, task_status, 3, reserved=not resolved)]},
            ]},
        ],
        "approvals": pending,
    }
    return RunOrchestrationSnapshotResponse.model_validate(payload).model_dump(mode="json")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": "deterministic-fixture"}


@app.get("/api/auth/session")
async def auth_session() -> dict[str, object]:
    return asdict(AuthSessionResponse(authenticated=True, username="s8-fixture-user"))


@app.get("/api/projects")
async def projects() -> list[dict[str, object]]:
    value = ProjectResponse(
        id=PROJECT_ID,
        name="S8 acceptance fixture",
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
    )
    return [value.model_dump(mode="json")]


@app.get("/api/s8-fixture")
async def fixture_info() -> dict[str, object]:
    return {"run_id": RUN_ID, "approval_request_id": REQUEST_ID, "llm": "deterministic, not real LLM"}


@app.get("/api/sessions")
async def sessions() -> list[dict[str, object]]:
    value = {
        "id": SESSION_ID,
        "project_id": PROJECT_ID,
        "title": "S8 fixture session",
        "active_run_id": RUN_ID,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "last_activity_at": NOW.isoformat(),
    }
    return [SessionResponse.model_validate(value).model_dump(mode="json")]


@app.get("/api/sessions/{session_id}")
async def session(session_id: str) -> dict[str, object]:
    if session_id != SESSION_ID: raise HTTPException(404, "session_not_found")
    return (await sessions())[0]


@app.get("/api/sessions/{session_id}/runs")
async def session_runs(session_id: str) -> list[dict[str, object]]:
    if session_id != SESSION_ID: raise HTTPException(404, "session_not_found")
    value = {
        "run_id": RUN_ID,
        "session_id": SESSION_ID,
        "message_id": "s8-fixture-message",
        "sequence": 1,
        "status": _snapshot()["run"]["status"],
        "root_thread_id": "s8-fixture-root",
        "error": None,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "started_at": NOW.isoformat(),
        "title": "S8 fixture two rounds",
    }
    return [RunSummaryResponse.model_validate(value).model_dump(mode="json")]


@app.get("/api/runs")
async def runs() -> dict[str, object]:
    return RunListResponse.model_validate({"items": await session_runs(SESSION_ID), "total": 1, "has_more": False}).model_dump(mode="json")


@app.get("/api/runs/{run_id}/orchestration")
async def orchestration(run_id: str) -> dict[str, object]:
    if run_id != RUN_ID: raise HTTPException(404, "run_not_found")
    return _snapshot()


@app.get("/api/sessions/{session_id}/runs/{run_id}/timeline")
async def timeline(session_id: str, run_id: str) -> dict[str, object]:
    if session_id != SESSION_ID or run_id != RUN_ID: raise HTTPException(404, "run_not_found")
    return RunTimelineResponse.model_validate({"run": (await session_runs(session_id))[0], "events": [], "next_after_seq": 0, "has_more": False, "links": {"retrieval_run_ids": [], "plan_ids": ["s8-fixture-plan-1", "s8-fixture-plan-2"], "task_ids": []}}).model_dump(mode="json")


@app.get("/api/approvals")
async def approvals() -> list[dict[str, object]]:
    request = _snapshot()["approvals"]
    if not request: return []
    return [{"approval_id": item["approval_id"], "tool_name": item["tool_name"], "arguments": item["arguments"], "risk_level": item["risk_level"], "approval_batch_id": request[0]["approval_batch_id"], "description": item["summary"], "session_id": SESSION_ID, "run_id": RUN_ID, "plan_id": request[0]["plan_id"], "task_id": request[0]["task_id"], "expires_at": request[0]["expires_at"]} for item in request[0]["items"] if item["status"] == "pending"]


@app.post("/api/approvals/{approval_id}/decision", status_code=202)
async def decision(approval_id: str, body: DecisionRequest) -> dict[str, object]:
    item_lookup = {"s8-fixture-approval-export": ITEMS[0], "s8-fixture-approval-notify": ITEMS[1]}
    item_id = item_lookup.get(approval_id)
    if item_id is None or item_id in _item_decisions: raise HTTPException(409, "approval_not_pending")
    if body.decision not in {"approved", "denied"}: raise HTTPException(422, "approval_decision_invalid")
    _item_decisions[item_id] = body.decision
    return {"status": "decision_recorded", "approval_id": approval_id, "decision": body.decision}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8799)
