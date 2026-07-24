"""Tests for AgentState schema."""

from langchain_core.messages import HumanMessage

from athena.core.graph.agent_state import AgentState


class TestAgentState:
    """Verify that the AgentState TypedDict is well-formed."""

    def test_minimal_state(self):
        """A minimal state dict should have all required fields resolvable."""
        state: AgentState = {
            "messages": [HumanMessage(content="hello")],
            "pending_tool_calls": None,
            "agent_iteration": 0,
            "status": "thinking",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }
        assert state["status"] == "thinking"
        assert state["agent_iteration"] == 0
        assert state["pending_tool_calls"] is None

    def test_with_tool_calls(self):
        """State with pending tool calls."""
        state: AgentState = {
            "messages": [HumanMessage(content="weather in Tokyo")],
            "pending_tool_calls": [
                {"id": "call_1", "name": "weather", "arguments": {"city": "Tokyo"}}
            ],
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "sess-1",
            "user_id": "user1",
            "channel": "web",
        }
        assert len(state["pending_tool_calls"]) == 1
        assert state["pending_tool_calls"][0]["name"] == "weather"

    def test_completed_state(self):
        """Terminal completed state."""
        state: AgentState = {
            "messages": [
                HumanMessage(content="hello"),
                HumanMessage(content="Hi! How can I help?"),  # AI response
            ],
            "pending_tool_calls": None,
            "agent_iteration": 1,
            "status": "completed",
            "session_id": "sess-1",
            "user_id": "user1",
            "channel": "web",
        }
        assert state["status"] == "completed"
        assert state["pending_tool_calls"] is None
