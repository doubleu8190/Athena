"""审批日志端点测试 — GET /approvals/logs + GET /approvals/stats."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.db.database import Database
from athena.models.approval import ApprovalDecision, ApprovalLog


def _make_log(
    log_id: str,
    session_id: str,
    tool_name: str,
    decision: ApprovalDecision,
    timestamp: datetime,
    risk_level: str = "medium",
) -> ApprovalLog:
    return ApprovalLog(
        id=log_id,
        session_id=session_id,
        tool_call_id=f"tc_{log_id}",
        tool_name=tool_name,
        arguments={"cmd": "echo hi"},
        risk_level=risk_level,
        decision=decision,
        decision_time_ms=120,
        timestamp=timestamp,
    )


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
async def db(db_path):
    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def client(db):
    from athena.gateway.routes.approval import router

    app = FastAPI()
    app.include_router(router)

    with patch(
        "athena.db.database.get_database",
        AsyncMock(return_value=db),
    ):
        yield TestClient(app)


@pytest.mark.asyncio
async def test_list_logs_newest_first(db, client):
    now = datetime.now()
    for i in range(3):
        await db.approval_logs.save(
            _make_log(f"l{i}", "s1", "exec_shell", ApprovalDecision.APPROVED, now + timedelta(minutes=i))
        )
    resp = client.get("/approvals/logs")
    assert resp.status_code == 200
    logs = resp.json()
    assert len(logs) == 3
    # 新→旧
    assert [l["id"] for l in logs] == ["l2", "l1", "l0"]
    assert logs[0]["decision"] == "approved"
    assert "arguments" in logs[0]


@pytest.mark.asyncio
async def test_list_logs_limit_offset_session_filter(db, client):
    now = datetime.now()
    for i in range(4):
        await db.approval_logs.save(
            _make_log(f"a{i}", "s1", "read_file", ApprovalDecision.APPROVED, now + timedelta(minutes=i))
        )
    await db.approval_logs.save(
        _make_log("b0", "s2", "exec_shell", ApprovalDecision.DENIED, now + timedelta(minutes=10))
    )

    resp = client.get(
        "/approvals/logs", params={"limit": 2, "offset": 1, "session_id": "s1"}
    )
    assert [l["id"] for l in resp.json()] == ["a2", "a1"]

    resp = client.get("/approvals/logs", params={"session_id": "s2"})
    logs = resp.json()
    assert len(logs) == 1
    assert logs[0]["id"] == "b0"
    assert logs[0]["decision"] == "denied"


@pytest.mark.asyncio
async def test_stats_counts(db, client):
    now = datetime.now()
    today = [
        _make_log("t0", "s1", "exec_shell", ApprovalDecision.APPROVED, now),
        _make_log("t1", "s1", "exec_shell", ApprovalDecision.APPROVED, now),
        _make_log("t2", "s1", "write_file", ApprovalDecision.DENIED, now),
        _make_log("t3", "s1", "write_file", ApprovalDecision.TIMEOUT, now),
    ]
    # 昨天的一条不计入今日
    yesterday = _make_log(
        "old", "s1", "exec_shell", ApprovalDecision.APPROVED, now - timedelta(days=1)
    )
    for log in [*today, yesterday]:
        await db.approval_logs.save(log)

    resp = client.get("/approvals/stats")
    assert resp.status_code == 200
    stats = resp.json()
    assert stats["today_total"] == 4
    assert stats["today_approved"] == 2
    assert stats["today_denied"] == 1
    assert stats["today_timeout"] == 1
    assert stats["approval_rate"] == round(2 / 3, 4)
