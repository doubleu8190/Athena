"""数据库 CRUD 测试."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime

import pytest

from athena.db.database import Database
from athena.models import (
    Message,
    MessageRole,
    Step,
    StepStatus,
    StepType,
    ToolCallRecord,
    ToolCallStatus,
)


@pytest.fixture
async def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    await database.connect()
    yield database
    await database.close()
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.mark.asyncio
async def test_create_and_get_session(db: Database):
    session = await db.sessions.create("sess-1", title="Test")
    assert session.id == "sess-1"
    assert session.title == "Test"
    assert session.status == "idle"

    fetched = await db.sessions.get("sess-1")
    assert fetched is not None
    assert fetched.title == "Test"
    assert fetched.status == "idle"


@pytest.mark.asyncio
async def test_list_sessions(db: Database):
    await db.sessions.create("s1")
    await db.sessions.create("s2")
    sessions = await db.sessions.list_all()
    assert len(sessions) == 2


@pytest.mark.asyncio
async def test_update_session_status(db: Database):
    await db.sessions.create("s1")
    await db.sessions.update("s1", status="running", run_id="20260730")
    fetched = await db.sessions.get("s1")
    assert fetched is not None
    assert fetched.status == "running"
    assert fetched.run_id == "20260730"


@pytest.mark.asyncio
async def test_save_and_get_messages(db: Database):
    await db.sessions.create("s1")
    await db.messages.save(Message(
        id="m1",
        session_id="s1",
        role=MessageRole.USER,
        content="hello",
        timestamp=datetime.now(),
    ))
    await db.messages.save(Message(
        id="m2",
        session_id="s1",
        role=MessageRole.ASSISTANT,
        content="hi",
        timestamp=datetime.now(),
    ))
    msgs = await db.messages.get_by_session("s1")
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[1].role == "assistant"


@pytest.mark.asyncio
async def test_save_step_and_query(db: Database):
    await db.sessions.create("s1")
    await db.steps.save(Step(
        id="step-1",
        session_id="s1",
        run_id="r1",
        step_number=1,
        step_type=StepType.LLM_CALL,
        status=StepStatus.RUNNING,
        started_at=datetime.now(),
    ))
    await db.steps.update("step-1", {
        "status": "completed", "duration_ms": 100.0,
        "llm_input_tokens": 50, "llm_output_tokens": 30,
    })
    steps = await db.steps.get_by_session("s1")
    assert len(steps) == 1
    assert steps[0].status == "completed"
    assert steps[0].llm_input_tokens == 50


@pytest.mark.asyncio
async def test_save_tool_call(db: Database):
    await db.sessions.create("s1")
    await db.steps.save(Step(
        id="step-1",
        session_id="s1",
        run_id="r1",
        step_number=1,
        step_type=StepType.TOOL_EXECUTION,
        status=StepStatus.RUNNING,
        started_at=datetime.now(),
    ))
    await db.tool_calls.save(ToolCallRecord(
        id="tc-1",
        session_id="s1",
        step_id="step-1",
        tool_name="read_file",
        arguments={"path": "/tmp"},
        status=ToolCallStatus.RUNNING,
        started_at=datetime.now(),
    ))
    await db.tool_calls.update("tc-1", {
        "status": "success", "raw_output": "data",
        "duration_ms": 50.0, "completed_at": datetime.now().isoformat(),
    })
    calls = await db.tool_calls.query("s1")
    assert len(calls) == 1
    assert calls[0].status == "success"


@pytest.mark.asyncio
async def test_delete_session_cascade(db: Database):
    await db.sessions.create("s1")
    await db.messages.save(Message(
        id="m1",
        session_id="s1",
        role=MessageRole.USER,
        content="x",
        timestamp=datetime.now(),
    ))
    await db.sessions.delete("s1")
    assert await db.sessions.get("s1") is None
    assert await db.messages.get_by_session("s1") == []


@pytest.mark.asyncio
async def test_baseline_schema_flat(db: Database):
    """验证 metadata_json 平铺后的新基线 schema.

    - messages 表含 step_id/tool_call_record_id/tool_name/type 列，且无 metadata_json
    - sessions/steps 表无 metadata_json
    - memories 表含 type/category/confidence/source 列
    - user_version 重置为 1（旧 v2-v5 迁移已废弃）
    """
    from sqlalchemy import text

    from athena.db.engine import get_session

    async with get_session() as session:
        result = await session.execute(text("PRAGMA table_info(messages)"))
        msg_cols = {row[1] for row in result.fetchall()}
        assert {"step_id", "tool_call_record_id", "tool_name", "type"} <= msg_cols
        assert "metadata_json" not in msg_cols

        for t in ("sessions", "steps"):
            result = await session.execute(text(f"PRAGMA table_info({t})"))
            cols = {row[1] for row in result.fetchall()}
            assert "metadata_json" not in cols

        result = await session.execute(text("PRAGMA table_info(memories)"))
        mem_cols = {row[1] for row in result.fetchall()}
        assert {"type", "category", "confidence", "source"} <= mem_cols

        result = await session.execute(text("PRAGMA user_version;"))
        assert result.scalar() == 1
