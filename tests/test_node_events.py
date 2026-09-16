"""LangGraph 节点生命周期事件的边界测试。"""

from __future__ import annotations

import pytest

from athena.runtime.node_events import instrument_graph_node


class _Publisher:
    """记录发布事件的最小异步事件发布器。"""

    def __init__(self) -> None:
        self.events = []

    async def publish(self, event) -> object:
        self.events.append(event)
        return event


@pytest.mark.asyncio
async def test_node_events_are_started_then_completed() -> None:
    """节点成功时必须按开始、完成顺序发布一对事件。"""
    publisher = _Publisher()

    async def node(state: dict[str, str]) -> dict[str, bool]:
        return {"ok": True}

    wrapped = instrument_graph_node("demo", node, event_publisher=publisher)
    result = await wrapped(
        {"session_id": "session-1", "run_id": "run-1"},
        {"metadata": {"session_id": "session-1", "run_id": "run-1"}},
    )

    assert result == {"ok": True}
    assert [event.event_type.value for event in publisher.events] == [
        "node.started",
        "node.completed",
    ]
    assert publisher.events[0].payload["node_name"] == "demo"
    assert publisher.events[1].payload["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_node_failure_publishes_structured_error_and_reraises() -> None:
    """节点异常时必须发布结构化失败事件并保持原异常语义。"""
    publisher = _Publisher()

    async def node(state: dict[str, str]) -> dict[str, bool]:
        raise ValueError("invalid input")

    wrapped = instrument_graph_node("failing_node", node, event_publisher=publisher)
    with pytest.raises(ValueError, match="invalid input"):
        await wrapped({"session_id": "session-1", "run_id": "run-1"}, {})

    assert [event.event_type.value for event in publisher.events] == [
        "node.started",
        "node.failed",
    ]
    detail = publisher.events[-1].payload["error_detail"]
    assert detail["error_type"] == "ValueError"
    assert detail["code"] == "graph_node_failed"
    assert detail["retryable"] is False


@pytest.mark.asyncio
async def test_event_publish_failure_does_not_change_node_result() -> None:
    """观测通道故障不能让业务节点失败。"""

    class BrokenPublisher:
        async def publish(self, event) -> object:
            raise RuntimeError("event store unavailable")

    async def node(state: dict[str, str]) -> dict[str, bool]:
        return {"ok": True}

    wrapped = instrument_graph_node(
        "observable_node", node, event_publisher=BrokenPublisher()
    )
    assert await wrapped({"session_id": "session-1"}, {}) == {"ok": True}
