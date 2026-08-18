"""Activity/tool-call projection API tests."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.infrastructure.sqlite.database import Database
from athena.models import Step, StepStatus, StepType, ToolCallRecord, ToolCallStatus
from tests.fakes import install_runtime


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
    from athena.gateway.routes.sessions import router

    app = FastAPI()
    app.include_router(router)
    tool_manager = MagicMock()
    tool_manager.get_risk_level.return_value = "high"
    install_runtime(app, db=db, tool_manager=tool_manager)

    return TestClient(app)


@pytest.mark.asyncio
async def test_tool_calls_response_matches_frontend_contract(db, client):
    session = await db.sessions.create("session-activity", "Activity")
    started_at = datetime.now()
    completed_at = datetime.now()

    await db.steps.save(
        Step(
            id="step-read-file",
            session_id=session.id,
            run_id="20260818120000123456",
            step_number=2,
            step_type=StepType.TOOL_EXECUTION,
            parent_step_id="step-llm",
            status=StepStatus.COMPLETED,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=12.5,
        )
    )
    await db.tool_calls.save(
        ToolCallRecord(
            id="tool-read-file",
            session_id=session.id,
            step_id="step-read-file",
            tool_name="read_file",
            arguments={"file_id": "file-1"},
            raw_output='{"file":{"filename":"screenshot.png","mime_type":"image/png"}}',
            status=ToolCallStatus.SUCCESS,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=12.5,
        )
    )

    resp = client.get(f"/sessions/{session.id}/tool_calls")

    assert resp.status_code == 200
    body = resp.json()
    assert body == [
        {
            "id": "tool-read-file",
            "session_id": session.id,
            "step_id": "step-read-file",
            "run_id": "20260818120000123456",
            "tool_name": "read_file",
            "arguments": {"file_id": "file-1"},
            "output": '{"file":{"filename":"screenshot.png","mime_type":"image/png"}}',
            "error": None,
            "error_stack": None,
            "status": "success",
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "duration_ms": 12.5,
            "risk_level": "high",
        }
    ]


def test_tool_calls_response_session_not_found(client):
    resp = client.get("/sessions/missing/tool_calls")
    assert resp.status_code == 404
