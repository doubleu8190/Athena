"""事件 Schema 测试."""

from __future__ import annotations

from athena.gateway.ws.events import EventType, build_event


def test_build_event_basic():
    ev = build_event(EventType.LLM_TOKEN, {"token": "hi"}, session_id="s1", run_id="r1")
    assert ev["type"] == "llm_token"
    assert ev["session_id"] == "s1"
    assert ev["run_id"] == "r1"
    assert ev["data"] == {"token": "hi"}
    assert "timestamp" in ev


def test_event_type_values():
    assert EventType.LLM_CALL_START == "llm_call_start"
    assert EventType.APPROVAL_REQUEST == "approval_request"
    assert EventType.TOOL_CALL_END == "tool_call_end"


def test_build_event_with_string_type():
    ev = build_event("custom_event", {"k": "v"})
    assert ev["type"] == "custom_event"
    assert ev["data"] == {"k": "v"}
