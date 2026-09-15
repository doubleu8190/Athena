"""审批等待和取消行为测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from athena.contracts.events import EventType
from athena.contracts.statuses import AgentApprovalDecision
from athena.gateway.approval import ApprovalManager
from athena.models.tool import RiskLevel


class _ApprovalStore:
    def __init__(self) -> None:
        self.resolutions: list[tuple[str, AgentApprovalDecision]] = []

    async def create_approval(self, **_: Any) -> None:
        return None

    async def get_approval(self, _: str) -> SimpleNamespace:
        return SimpleNamespace(status="pending", decision=None)

    async def resolve_approval(
        self, approval_id: str, decision: AgentApprovalDecision
    ) -> bool:
        self.resolutions.append((approval_id, decision))
        return True


class _EventPublisher:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, event) -> object:
        self.events.append(event)
        return event


@pytest.mark.asyncio
async def test_cancelled_approval_is_expired_and_not_left_pending() -> None:
    store = _ApprovalStore()
    publisher = _EventPublisher()
    manager = ApprovalManager(
        event_publisher=publisher,
        db=None,  # type: ignore[arg-type]
        agent_store=store,  # type: ignore[arg-type]
        approval_timeout=120,
    )

    request = await manager.request_approval(
        tool_name="write_file",
        arguments={"path": "result.csv"},
        risk_level=RiskLevel.MEDIUM,
        session_id="session-1",
        run_id="run-1",
        tool_call_id="tool-1",
    )
    waiter = asyncio.create_task(manager.wait_for_decision(request.id, 120))
    await asyncio.sleep(0)
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert store.resolutions == [(request.id, AgentApprovalDecision.EXPIRED)]
    assert publisher.events[-1].event_type == EventType.APPROVAL_EXPIRED
    assert manager.get_pending("session-1") == []
