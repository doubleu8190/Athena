"""Tests for agent graph routing functions."""

from athena.core.graph.agent_routing import after_agent, after_tools, MAX_AGENT_ITERATIONS


class TestAfterAgent:
    """Tests for the after_agent conditional edge function."""

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

    def test_has_tool_calls_routes_to_tools(self):
        """Agent with pending tool calls should route to tools node."""
        state = {
            "status": "executing",
            "pending_tool_calls": [{"id": "call_1", "name": "weather", "arguments": {}}],
            "agent_iteration": 1,
        }
        assert after_agent(state) == "tools"

    def test_max_iterations_exceeded_ends(self):
        """Agent should stop when max iterations reached."""
        state = {
            "status": "executing",
            "pending_tool_calls": [{"id": "call_1", "name": "weather", "arguments": {}}],
            "agent_iteration": MAX_AGENT_ITERATIONS,
        }
        assert after_agent(state) == "__end__"

    def test_one_below_max_still_routes_to_tools(self):
        """Agent one below max iterations should still route to tools."""
        state = {
            "status": "executing",
            "pending_tool_calls": [{"id": "call_1", "name": "weather", "arguments": {}}],
            "agent_iteration": MAX_AGENT_ITERATIONS - 1,
        }
        assert after_agent(state) == "tools"


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
