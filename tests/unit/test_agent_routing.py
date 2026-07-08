"""Tests for agent graph routing functions."""

from langgraph.types import Send

from athena.core.graph.agent_routing import after_agent, after_confirm, after_tools, MAX_AGENT_ITERATIONS


class TestAfterAgent:
    """Tests for the after_agent routing function."""

    def test_completed_status_ends(self):
        """Agent with completed status should route to END."""
        state = {"status": "completed", "pending_tool_calls": None, "agent_iteration": 1}
        assert after_agent(state) == "__end__"

    def test_failed_status_ends(self):
        """Agent with failed status should route to END."""
        state = {"status": "failed", "pending_tool_calls": None, "agent_iteration": 1}
        assert after_agent(state) == "__end__"

    def test_no_tool_calls_ends(self):
        """Agent with no pending tool calls should route to END."""
        state = {"status": "executing", "pending_tool_calls": None, "agent_iteration": 1}
        assert after_agent(state) == "__end__"

    def test_has_tool_calls_routes_to_confirm(self):
        """Agent with pending tool calls should route to confirm node."""
        state = {
            "status": "executing",
            "pending_tool_calls": [{"id": "call_1", "name": "weather", "arguments": {}}],
            "agent_iteration": 1,
        }
        result = after_agent(state)
        assert result == "confirm"

    def test_multiple_tool_calls_routes_to_confirm(self):
        """Agent with multiple tool calls should route to confirm node."""
        state = {
            "status": "executing",
            "pending_tool_calls": [
                {"id": "call_1", "name": "weather", "arguments": {}},
                {"id": "call_2", "name": "search", "arguments": {}},
            ],
            "agent_iteration": 1,
        }
        result = after_agent(state)
        assert result == "confirm"

    def test_max_iterations_still_routes_pending_tools(self):
        """Even at max iterations, pending tool_calls must be routed to confirm."""
        state = {
            "status": "executing",
            "pending_tool_calls": [{"id": "call_1", "name": "weather", "arguments": {}}],
            "agent_iteration": MAX_AGENT_ITERATIONS,
        }
        result = after_agent(state)
        assert result == "confirm"

    def test_one_below_max_still_routes_to_confirm(self):
        """Agent one below max iterations should still route to confirm."""
        state = {
            "status": "executing",
            "pending_tool_calls": [{"id": "call_1", "name": "weather", "arguments": {}}],
            "agent_iteration": MAX_AGENT_ITERATIONS - 1,
        }
        result = after_agent(state)
        assert result == "confirm"


class TestAfterConfirm:
    """Tests for the after_confirm routing function."""

    def test_confirmed_calls_return_sends(self):
        """Confirmed tool calls should return Send() objects to tools node."""
        state = {
            "confirmed_tool_calls": [{"id": "call_1", "name": "weather", "arguments": {}}],
            "pending_tool_calls": None,
        }
        result = after_confirm(state)
        assert len(result) == 1
        assert isinstance(result[0], Send)
        assert result[0].node == "tools"

    def test_multiple_confirmed_calls_return_multiple_sends(self):
        """Multiple confirmed calls should return one Send per call."""
        state = {
            "confirmed_tool_calls": [
                {"id": "call_1", "name": "weather", "arguments": {}},
                {"id": "call_2", "name": "search", "arguments": {}},
            ],
            "pending_tool_calls": None,
        }
        result = after_confirm(state)
        assert len(result) == 2
        assert all(isinstance(s, Send) for s in result)
        assert all(s.node == "tools" for s in result)

    def test_no_confirmed_calls_ends(self):
        """No confirmed calls should return empty list (END)."""
        state = {
            "confirmed_tool_calls": None,
            "pending_tool_calls": None,
        }
        result = after_confirm(state)
        assert result == []

    def test_empty_confirmed_list_ends(self):
        """Empty confirmed list should return empty list (END)."""
        state = {
            "confirmed_tool_calls": [],
            "pending_tool_calls": None,
        }
        result = after_confirm(state)
        assert result == []


class TestAfterTools:
    """Tests for the after_tools conditional edge function."""

    def test_tools_routes_back_to_agent(self):
        """After tools complete, always route back to agent for synthesis."""
        state = {
            "status": "executing",
            "pending_tool_calls": None,
            "agent_iteration": 1,
        }
        assert after_tools(state) == "agent"

    def test_failed_status_ends(self):
        """Tools with failed status should stop."""
        state = {
            "status": "failed",
            "pending_tool_calls": None,
            "agent_iteration": 1,
        }
        assert after_tools(state) == "__end__"
