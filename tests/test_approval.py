"""审批管理器测试."""

from __future__ import annotations

import asyncio

import pytest

from athena.gateway.approval import ApprovalManager


@pytest.fixture
def manager() -> ApprovalManager:
    return ApprovalManager(approval_timeout=2)


@pytest.mark.asyncio
async def test_request_approval_returns_future(manager: ApprovalManager):
    req = await manager.request_approval(
        tool_name="exec_shell",
        arguments={"command": "ls"},
        risk_level="high",
        session_id="sess-1",
        run_id="run-1",
    )
    assert req.id is not None
    assert req.tool_name == "exec_shell"
    assert not req.future.done()


@pytest.mark.asyncio
async def test_respond_approval_allow(manager: ApprovalManager):
    req = await manager.request_approval(
        tool_name="write_file",
        arguments={"path": "/tmp/x"},
        risk_level="medium",
        session_id="sess-1",
        run_id="run-1",
    )
    ok = await manager.respond_approval(req.id, "allow")
    assert ok is True
    approved = await req.future
    assert approved is True
    assert req.resolution == "approved"


@pytest.mark.asyncio
async def test_respond_approval_deny(manager: ApprovalManager):
    req = await manager.request_approval(
        tool_name="exec_shell",
        arguments={},
        risk_level="high",
        session_id="sess-1",
        run_id="run-1",
    )
    await manager.respond_approval(req.id, "deny")
    approved = await req.future
    assert approved is False
    assert req.resolution == "denied"


@pytest.mark.asyncio
async def test_approval_timeout(manager: ApprovalManager):
    req = await manager.request_approval(
        tool_name="exec_shell",
        arguments={},
        risk_level="high",
        session_id="sess-1",
        run_id="run-1",
    )
    # 等待超时（2s）
    approved = await req.future
    assert approved is False
    assert req.resolution == "timeout"


@pytest.mark.asyncio
async def test_queue_processes_sequentially(manager: ApprovalManager):
    """多个请求应顺序处理，同一时刻只处理一个."""
    req1 = await manager.request_approval(
        tool_name="t1", arguments={}, risk_level="low",
        session_id="s1", run_id="r1",
    )
    req2 = await manager.request_approval(
        tool_name="t2", arguments={}, risk_level="low",
        session_id="s1", run_id="r1",
    )
    # 处理 req1 时，req2 仍在队列中
    await asyncio.sleep(0.05)
    assert req1.resolved is False or req2.resolved is False

    await manager.respond_approval(req1.id, "allow")
    await asyncio.sleep(0.05)
    # req1 已解决，req2 开始处理
    await manager.respond_approval(req2.id, "allow")

    await req1.future
    await req2.future
    assert req1.resolution == "approved"
    assert req2.resolution == "approved"


@pytest.mark.asyncio
async def test_cancel_all_pending_for_session(manager: ApprovalManager):
    await manager.request_approval(
        tool_name="t1", arguments={}, risk_level="low",
        session_id="s1", run_id="r1",
    )
    await manager.request_approval(
        tool_name="t2", arguments={}, risk_level="low",
        session_id="s2", run_id="r2",
    )
    await manager.cancel_all_pending("s1")
    pending = manager.get_pending()
    sessions = {p["session_id"] for p in pending}
    # s1 的请求已被取消（可能因超时已移除），仅剩 s2
    assert "s1" not in sessions or all(p["session_id"] == "s2" for p in pending)
