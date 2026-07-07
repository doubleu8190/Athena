"""Tests for agent graph routing functions."""

from langgraph.types import Send

from athena.core.graph.agent_routing import after_agent, after_tools, MAX_AGENT_ITERATIONS


class TestAfterAgent:
    """Tests for the after_agent routing function."""

    def test_completed_status_ends(self):
        """Agent with completed status should return empty list (END)."""
        state = {"status": "completed", "pending_tool_calls": None, "agent_iteration": 1}
        assert after_agent(state) == []

    def test_failed_status_ends(self):
        """Agent with failed status should return empty list (END)."""
        state = {"status": "failed", "pending_tool_calls": None, "agent_iteration": 1}
        assert after_agent(state) == []

    def test_no_tool_calls_ends(self):
        """Agent with no pending tool calls should return empty list (END)."""
        state = {"status": "executing", "pending_tool_calls": None, "agent_iteration": 1}
        assert after_agent(state) == []

    def test_has_tool_calls_returns_sends(self):
        """Agent with pending tool calls should return Send() objects."""
        state = {
            "status": "executing",
            "pending_tool_calls": [{"id": "call_1", "name": "weather", "arguments": {}}],
            "agent_iteration": 1,
        }
        result = after_agent(state)
        assert len(result) == 1
        assert isinstance(result[0], Send)
        assert result[0].node == "tools"

    def test_multiple_tool_calls_returns_multiple_sends(self):
        """Agent with multiple tool calls should return one Send per call."""
        state = {
            "status": "executing",
            "pending_tool_calls": [
                {"id": "call_1", "name": "weather", "arguments": {}},
                {"id": "call_2", "name": "search", "arguments": {}},
            ],
            "agent_iteration": 1,
        }
        result = after_agent(state)
        assert len(result) == 2
        assert all(isinstance(s, Send) for s in result)
        assert all(s.node == "tools" for s in result)

    def test_max_iterations_still_routes_pending_tools(self):
        """Even at max iterations, pending tool_calls must be executed.

        The iteration limit is enforced at the START of agent_node (before the
        LLM call), not in after_agent.  If the agent already produced
        tool_calls they must be routed to tools — dropping them silently
        would lose work.
        """
        state = {
            "status": "executing",
            "pending_tool_calls": [{"id": "call_1", "name": "weather", "arguments": {}}],
            "agent_iteration": MAX_AGENT_ITERATIONS,
        }
        result = after_agent(state)
        assert len(result) == 1
        assert isinstance(result[0], Send)

    def test_one_below_max_still_routes_to_tools(self):
        """Agent one below max iterations should still route to tools."""
        state = {
            "status": "executing",
            "pending_tool_calls": [{"id": "call_1", "name": "weather", "arguments": {}}],
            "agent_iteration": MAX_AGENT_ITERATIONS - 1,
        }
        result = after_agent(state)
        assert len(result) == 1
        assert isinstance(result[0], Send)


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
