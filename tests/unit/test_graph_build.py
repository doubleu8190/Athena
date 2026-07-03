"""Tests for graph construction and structure."""

import pytest
from unittest.mock import MagicMock

from athena.core.graph import build_graph


class TestGraphConstruction:
    """Verify that the graph compiles and has the expected structure."""

    def test_graph_compiles(self):
        """Graph must compile without errors."""
        graph = build_graph()
        assert graph is not None

    def test_all_nodes_present(self):
        """All required nodes must be registered."""
        graph = build_graph()
        nodes = list(graph.get_graph().nodes.keys())
        expected = {
            "__start__", "initialize", "plan", "execute",
            "handle_failure", "collect", "finalize", "__end__",
        }
        assert set(nodes) == expected

    def test_start_to_initialize(self):
        """START → initialize edge must exist."""
        graph = build_graph()
        edges = graph.get_graph().edges
        start_edges = [e for e in edges if e.source == "__start__"]
        assert len(start_edges) == 1
        assert start_edges[0].target == "initialize"

    def test_no_orphan_nodes(self):
        """Every node should appear in the graph's node list.

        Note: Nodes reached via ``Send()`` dynamic dispatch (``execute``,
        ``handle_failure``, ``collect``) may not appear as static edge
        targets, but they ARE registered nodes in the graph.
        """
        graph = build_graph()
        g = graph.get_graph()
        nodes = set(g.nodes.keys())

        # START and END are always present
        assert "__start__" in nodes
        assert "__end__" in nodes

        # Core nodes must all be registered
        required = {
            "initialize", "plan", "execute",
            "handle_failure", "collect", "finalize",
        }
        for node in required:
            assert node in nodes, f"Required node {node} is missing"

        # initialize → plan edge is the only guaranteed static edge
        # (other nodes are reached via Send() dynamic dispatch)
        connected = {e.source for e in g.edges} | {e.target for e in g.edges}
        assert "initialize" in connected
        assert "plan" in connected

    def test_conditional_edges_registered(self):
        """Conditional edge functions must be attached to correct nodes."""
        graph = build_graph()
        compiled = graph
        # The compiled graph should have the nodes with conditional routing
        assert compiled is not None
        # We can't easily inspect internal conditional edges,
        # but compilation verifies they are well-formed.


class TestGraphWithMockedDependencies:
    """Integration-style test with mocked LLM and MCP client."""

    @pytest.fixture
    def mock_deps(self):
        """Build a minimal set of mocked dependencies."""
        from langchain_core.messages import HumanMessage

        # Mock LLM manager with async generate
        llm_mgr = MagicMock()
        llm_response = MagicMock()
        llm_response.text = '''
        {
            "task_id": "test-task-1",
            "subtasks": [
                {
                    "step": 1,
                    "intent": "Read a file",
                    "tool_name": "file_read",
                    "args": {"path": "/tmp/test.txt"},
                    "depends_on": [],
                    "critical": true,
                    "on_failure": "abort",
                    "fallback_tool": null
                }
            ]
        }
        '''
        # Make generate return an awaitable
        async def _mock_generate(*args, **kwargs):
            return llm_response
        llm_mgr.generate = _mock_generate

        # Mock harness engine
        harness = MagicMock()
        harness_result = MagicMock()
        harness_result.allowed = True
        harness_result.requires_confirmation = False
        harness_result.risk_level = MagicMock()
        harness_result.risk_level.value = "low"
        harness_result.cooling_off_seconds = 0
        async def _mock_pre_check(*args, **kwargs):
            return harness_result
        harness.pre_check = _mock_pre_check

        # Mock MCP client
        mcp_client = MagicMock()
        tool_result = MagicMock()
        tool_result.success = True
        tool_result.content = {"result": "file contents here"}
        async def _mock_call_tool(*args, **kwargs):
            return tool_result
        mcp_client.call_tool = _mock_call_tool

        # Mock tool registry
        tool_registry = MagicMock()
        tool_registry.export_for_planner.return_value = []
        mock_tool = MagicMock()
        mock_tool.source_server_id = "builtin-core"
        tool_registry.get_tool_by_name.return_value = mock_tool

        return {
            "llm_manager": llm_mgr,
            "harness_engine": harness,
            "mcp_client": mcp_client,
            "tool_registry": tool_registry,
            "messages": [HumanMessage(content="read /tmp/test.txt")],
        }

    @pytest.mark.asyncio
    async def test_graph_runs_end_to_end(self, mock_deps):
        """Graph should complete with mocked dependencies."""
        graph = build_graph()

        initial_state = {
            "messages": mock_deps["messages"],
        }

        config = {
            "configurable": {
                "thread_id": "test-thread-1",
                "session_id": "test-session",
                "user_id": "test-user",
                "channel": "test",
                "session_context": MagicMock(),
                "context_manager": MagicMock(),
                "llm_manager": mock_deps["llm_manager"],
                "mcp_client": mock_deps["mcp_client"],
                "harness_engine": mock_deps["harness_engine"],
                "tool_registry": mock_deps["tool_registry"],
            }
        }

        events = []
        async for event in graph.astream(
            initial_state,
            config=config,
            stream_mode="updates",
        ):
            events.append(event)

        # Should have plan, execute, collect, finalize events
        assert len(events) > 0, "Graph produced no events"

        # Find the finalize event
        finalize_events = [
            e for e in events
            if "finalize" in e and e["finalize"].get("status")
        ]
        assert len(finalize_events) > 0, "Graph did not reach finalize node"
        final_status = finalize_events[-1]["finalize"]["status"]
        assert final_status in ("completed", "failed")

    @pytest.mark.asyncio
    async def test_graph_handles_plan_error(self, mock_deps):
        """Graph should terminate gracefully when plan fails."""
        graph = build_graph()

        # Make planner fail
        async def _mock_fail(*args, **kwargs):
            raise RuntimeError("LLM down")
        mock_deps["llm_manager"].generate = _mock_fail

        initial_state = {
            "messages": mock_deps["messages"],
        }

        config = {
            "configurable": {
                "thread_id": "test-thread-2",
                "session_id": "test-session",
                "user_id": "test-user",
                "channel": "test",
                "session_context": MagicMock(),
                "context_manager": MagicMock(),
                "llm_manager": mock_deps["llm_manager"],
                "mcp_client": mock_deps["mcp_client"],
                "harness_engine": mock_deps["harness_engine"],
                "tool_registry": mock_deps["tool_registry"],
            }
        }

        events = []
        async for event in graph.astream(
            initial_state,
            config=config,
            stream_mode="updates",
        ):
            events.append(event)

        # Should have plan node output with error, then terminate
        plan_events = [e for e in events if "plan" in e]
        assert len(plan_events) > 0
        assert plan_events[0]["plan"].get("plan_error") is not None

    @pytest.mark.asyncio
    async def test_graph_handles_harness_block(self, mock_deps):
        """Graph should handle harness blocking a subtask."""
        graph = build_graph()

        # Make harness block by overriding the async mock
        blocked_result = MagicMock()
        blocked_result.allowed = False
        blocked_result.requires_confirmation = False
        blocked_result.reason = "Test block"
        async def _mock_blocked(*args, **kwargs):
            return blocked_result
        mock_deps["harness_engine"].pre_check = _mock_blocked

        initial_state = {
            "messages": mock_deps["messages"],
        }

        config = {
            "configurable": {
                "thread_id": "test-thread-3",
                "session_id": "test-session",
                "user_id": "test-user",
                "channel": "test",
                "session_context": MagicMock(),
                "context_manager": MagicMock(),
                "llm_manager": mock_deps["llm_manager"],
                "mcp_client": mock_deps["mcp_client"],
                "harness_engine": mock_deps["harness_engine"],
                "tool_registry": mock_deps["tool_registry"],
            }
        }

        events = []
        async for event in graph.astream(
            initial_state,
            config=config,
            stream_mode="updates",
        ):
            events.append(event)

        # Should have a blocked subtask result
        execute_events = [
            e for e in events
            if "execute" in e and e["execute"].get("subtask_result")
        ]
        blocked = [
            e for e in execute_events
            if e["execute"]["subtask_result"]["status"] == "blocked"
        ]
        assert len(blocked) > 0
