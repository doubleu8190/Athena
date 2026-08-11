"""用户主动恢复端点测试 — REST API + 恢复上下文.

测试覆盖：
- GET /sessions/interrupted：列出中断会话及恢复上下文
- POST /sessions/{id}/recover：手动触发恢复
- POST /sessions/{id}/abandon：放弃并重置会话
- 边界条件：状态校验、不存在的会话、重复恢复
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from athena.db.database import Database
from athena.models.step import Step, StepStatus, StepType
from athena.models.tool import ToolCallRecord, ToolCallStatus


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
def app(db, db_path):
    """创建测试用 FastAPI 应用."""
    from athena.gateway.routes.sessions import router

    # Mock get_database to return our test db
    async def mock_get_db():
        return db

    # Mock get_workflow
    mock_workflow = AsyncMock()

    from fastapi import FastAPI

    application = FastAPI()
    application.include_router(router)

    # Patch dependencies
    import athena.gateway.routes.sessions as sessions_mod

    original_get_db = sessions_mod._get_db
    original_get_workflow = sessions_mod._get_workflow

    sessions_mod._get_db = mock_get_db
    sessions_mod._get_workflow = AsyncMock(return_value=mock_workflow)

    yield application

    sessions_mod._get_db = original_get_db
    sessions_mod._get_workflow = original_get_workflow


@pytest.fixture
def client(app):
    return TestClient(app)


# ── GET /sessions/interrupted ─────────────────────────────────


@pytest.mark.asyncio
async def test_list_interrupted_empty(client, db):
    """没有中断会话时返回空列表."""
    resp = client.get("/sessions/interrupted")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_interrupted_with_sessions(client, db):
    """中断会话包含恢复上下文信息."""
    # 创建一个中断的会话
    await db.sessions.create("s1", title="中断的会话")
    await db.sessions.update("s1", status="interrupted")

    # 添加一条用户消息
    from athena.models import Message, MessageRole
    await db.messages.save(Message(
        id="m1",
        session_id="s1",
        role=MessageRole.USER,
        content="请帮我读取 config.yaml 文件",
        timestamp=datetime.now(),
    ))

    # 添加一个中断的工具调用
    await db.tool_calls.save(ToolCallRecord(
        id="tc1",
        session_id="s1",
        step_id="step1",
        tool_name="read_file",
        arguments={"path": "/workspace/config.yaml"},
        status=ToolCallStatus.RUNNING,
        started_at=datetime.now(),
    ))

    resp = client.get("/sessions/interrupted")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1

    info = data[0]
    assert info["session_id"] == "s1"
    assert info["title"] == "中断的会话"
    assert info["status"] == "interrupted"
    assert info["last_message_role"] == "user"
    assert "config.yaml" in info["last_message_preview"]
    assert len(info["pending_tools"]) == 1
    assert info["pending_tools"][0]["tool_name"] == "read_file"
    assert "recovery_hint" in info


@pytest.mark.asyncio
async def test_list_interrupted_includes_failed(client, db):
    """失败的会话也应出现在列表中."""
    await db.sessions.create("s-failed", title="失败的会话")
    await db.sessions.update("s-failed", status="failed")

    resp = client.get("/sessions/interrupted")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["status"] == "failed"


# ── POST /sessions/{id}/recover ───────────────────────────────


@pytest.mark.asyncio
async def test_recover_interrupted_session(client, db):
    """恢复中断的会话."""
    await db.sessions.create("s-recover", title="可恢复")
    await db.sessions.update("s-recover", status="interrupted")

    resp = client.post("/sessions/s-recover/recover")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "s-recover"
    assert data["status"] == "recovering"


@pytest.mark.asyncio
async def test_recover_running_session(client, db):
    """running 状态的会话也可以恢复（僵尸进程遗留）."""
    await db.sessions.create("s-running", title="僵尸会话")
    await db.sessions.update("s-running", status="running")

    resp = client.post("/sessions/s-running/recover")
    assert resp.status_code == 200
    assert resp.json()["status"] == "recovering"


@pytest.mark.asyncio
async def test_recover_idle_session_rejected(client, db):
    """idle 状态的会话不能恢复."""
    await db.sessions.create("s-idle", title="正常会话")

    resp = client.post("/sessions/s-idle/recover")
    assert resp.status_code == 409
    assert "idle" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_recover_nonexistent_session(client, db):
    """不存在的会话返回 404."""
    resp = client.post("/sessions/nonexistent/recover")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_recover_already_recovering(client, db):
    """已经在恢复中的会话不能重复触发."""
    await db.sessions.create("s-rec", title="恢复中")
    await db.sessions.update("s-rec", status="recovering")

    resp = client.post("/sessions/s-rec/recover")
    assert resp.status_code == 409


# ── POST /sessions/{id}/abandon ───────────────────────────────


@pytest.mark.asyncio
async def test_abandon_interrupted_session(client, db):
    """放弃中断的会话 — 重置为 idle."""
    await db.sessions.create("s-abort", title="要放弃的")
    await db.sessions.update("s-abort", status="interrupted")

    # 添加孤儿 step
    await db.steps.save(Step(
        id="step-orphan",
        session_id="s-abort",
        run_id="run1",
        step_number=1,
        step_type=StepType.TOOL_EXECUTION,
        status=StepStatus.RUNNING,
        started_at=datetime.now(),
    ))

    resp = client.post("/sessions/s-abort/abandon")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "idle"
    assert "重置" in data["message"]

    # 验证会话状态已重置
    session = await db.sessions.get("s-abort")
    assert session.status.value == "idle"


@pytest.mark.asyncio
async def test_abandon_failed_session(client, db):
    """失败的会话也可以放弃."""
    await db.sessions.create("s-fail", title="失败的")
    await db.sessions.update("s-fail", status="failed")

    resp = client.post("/sessions/s-fail/abandon")
    assert resp.status_code == 200
    assert resp.json()["status"] == "idle"


@pytest.mark.asyncio
async def test_abandon_idle_session_rejected(client, db):
    """idle 状态的会话无需放弃."""
    await db.sessions.create("s-ok", title="正常的")

    resp = client.post("/sessions/s-ok/abandon")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_abandon_nonexistent_session(client, db):
    """不存在的会话返回 404."""
    resp = client.post("/sessions/nonexistent/abandon")
    assert resp.status_code == 404


# ── 恢复上下文构建测试 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_hint_user_unanswered(client, db):
    """用户消息未响应时的恢复提示."""
    await db.sessions.create("s-hint1", title="hint")
    await db.sessions.update("s-hint1", status="interrupted")

    from athena.models import Message, MessageRole
    await db.messages.save(Message(
        id="m-hint",
        session_id="s-hint1",
        role=MessageRole.USER,
        content="hello",
        timestamp=datetime.now(),
    ))

    resp = client.get("/sessions/interrupted")
    hint = resp.json()[0]["recovery_hint"]
    assert "用户消息未得到响应" in hint


@pytest.mark.asyncio
async def test_recovery_hint_tool_interrupted(client, db):
    """工具中断时的恢复提示包含工具名."""
    await db.sessions.create("s-hint2", title="hint")
    await db.sessions.update("s-hint2", status="interrupted")

    from athena.models import Message, MessageRole
    await db.messages.save(Message(
        id="m-hint2",
        session_id="s-hint2",
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[{"name": "exec_shell", "args": {"command": "ls"}}],
        timestamp=datetime.now(),
    ))

    await db.tool_calls.save(ToolCallRecord(
        id="tc-hint",
        session_id="s-hint2",
        step_id="step-hint",
        tool_name="exec_shell",
        arguments={"command": "ls"},
        status=ToolCallStatus.RUNNING,
        started_at=datetime.now(),
    ))

    resp = client.get("/sessions/interrupted")
    info = resp.json()[0]
    assert "exec_shell" in info["recovery_hint"]
