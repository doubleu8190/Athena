"""审批请求持久化和事件发布测试。"""

from __future__ import annotations

from typing import Any

import pytest

from athena.contracts.events import EventType
from athena.gateway.approval import ApprovalManager
from athena.models.tool import RiskLevel


class _ApprovalStore:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create_approval(self, **kwargs: Any) -> bool:
        self.created.append(kwargs)
        return True

class _EventPublisher:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, event) -> object:
        self.events.append(event)
        return event


@pytest.mark.asyncio
async def test_request_approval_persists_and_publishes_event() -> None:
    store = _ApprovalStore()
    publisher = _EventPublisher()
    manager = ApprovalManager(
        event_publisher=publisher,
        agent_store=store,  # type: ignore[arg-type]
    )

    request = await manager.request_approval(
        tool_name="write_file",
        arguments={"path": "result.csv"},
        risk_level=RiskLevel.MEDIUM,
        session_id="session-1",
        run_id="run-1",
        tool_call_id="tool-1",
    )
    assert store.created[0]["approval_id"] == request.id
    assert store.created[0]["approval_batch_id"] is None
    assert publisher.events[-1].event_type == EventType.APPROVAL_REQUIRED
