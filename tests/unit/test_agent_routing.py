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

    def test_has_tool_calls_routes_to_precheck(self):
        """Agent with pending tool calls should route to precheck node."""
        state = {
            "status": "executing",
            "pending_tool_calls": [{"id": "call_1", "name": "weather", "arguments": {}}],
            "agent_iteration": 1,
        }
        result = after_agent(state)
        assert result == "precheck"

    def test_multiple_tool_calls_routes_to_precheck(self):
        """Agent with multiple tool calls should route to precheck node."""
        state = {
            "status": "executing",
            "pending_tool_calls": [
                {"id": "call_1", "name": "weather", "arguments": {}},
                {"id": "call_2", "name": "search", "arguments": {}},
            ],
            "agent_iteration": 1,
        }
        result = after_agent(state)
        assert result == "precheck"

    def test_max_iterations_still_routes_pending_tools(self):
        """Even at max iterations, pending tool_calls must be routed to precheck."""
        state = {
            "status": "executing",
            "pending_tool_calls": [{"id": "call_1", "name": "weather", "arguments": {}}],
            "agent_iteration": MAX_AGENT_ITERATIONS,
        }
        result = after_agent(state)
        assert result == "precheck"

    def test_one_below_max_still_routes_to_precheck(self):
        """Agent one below max iterations should still route to precheck."""
        state = {
            "status": "executing",
            "pending_tool_calls": [{"id": "call_1", "name": "weather", "arguments": {}}],
            "agent_iteration": MAX_AGENT_ITERATIONS - 1,
        }
        result = after_agent(state)
        assert result == "precheck"


class TestAfterConfirm:
    """Tests for the after_confirm routing function."""

    def test_confirmed_calls_return_sends(self):
        """Confirmed tool calls should return Send() objects to tools node."""
        state = {
            "confirmed_tool_calls": [{"id": "call_1", "name": "weather", "arguments": {}}],
            "allowed_tool_calls": None,
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
            "allowed_tool_calls": None,
            "pending_tool_calls": None,
        }
        result = after_confirm(state)
        assert len(result) == 2
        assert all(isinstance(s, Send) for s in result)
        assert all(s.node == "tools" for s in result)

    def test_allowed_calls_return_sends(self):
        """Allowed tool calls (no confirmation needed) should return Send() objects."""
        state = {
            "confirmed_tool_calls": None,
            "allowed_tool_calls": [{"id": "call_1", "name": "weather", "arguments": {}}],
            "pending_tool_calls": None,
        }
        result = after_confirm(state)
        assert len(result) == 1
        assert isinstance(result[0], Send)
        assert result[0].node == "tools"

    def test_merges_allowed_and_confirmed(self):
        """Allowed + confirmed calls should be merged into a single fan-out."""
        state = {
            "confirmed_tool_calls": [{"id": "call_2", "name": "file_write", "arguments": {}}],
            "allowed_tool_calls": [{"id": "call_1", "name": "weather", "arguments": {}}],
            "pending_tool_calls": None,
        }
        result = after_confirm(state)
        assert len(result) == 2
        assert all(isinstance(s, Send) for s in result)
        # Allowed comes first, then confirmed
        assert result[0].node == "tools"
        assert result[1].node == "tools"

    def test_no_calls_returns_summarize(self):
        """No confirmed or allowed calls should return summarize (agent processes rejections)."""
        state = {
            "confirmed_tool_calls": None,
            "allowed_tool_calls": None,
            "pending_tool_calls": None,
        }
        result = after_confirm(state)
        assert result == "summarize"

    def test_empty_lists_returns_summarize(self):
        """Empty confirmed and allowed lists should return summarize (agent processes rejections)."""
        state = {
            "confirmed_tool_calls": [],
            "allowed_tool_calls": [],
            "pending_tool_calls": None,
        }
        result = after_confirm(state)
        assert result == "summarize"


class TestAfterTools:
    """Tests for the after_tools conditional edge function."""

    def test_tools_routes_back_to_summarize(self):
        """After tools complete, route back to summarize (then agent)."""
        state = {
            "status": "executing",
            "pending_tool_calls": None,
            "agent_iteration": 1,
        }
        assert after_tools(state) == "summarize"

    def test_failed_status_ends(self):
        """Tools with failed status should stop."""
        state = {
            "status": "failed",
            "pending_tool_calls": None,
            "agent_iteration": 1,
        }
        assert after_tools(state) == "__end__"
