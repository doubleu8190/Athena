"""Tests for agent graph construction and end-to-end execution."""

import pytest
from unittest.mock import MagicMock, PropertyMock, patch, AsyncMock

from athena.core.graph import build_agent_graph


class TestAgentGraphConstruction:
    """Verify that the agent graph compiles with correct structure."""

    def test_graph_compiles(self):
        """Graph must compile without errors."""
        graph = build_agent_graph()
        assert graph is not None

    def test_nodes_present(self):
        """Agent, precheck, confirm, and tools nodes must be registered."""
        graph = build_agent_graph()
        nodes = list(graph.get_graph().nodes.keys())
        expected = {"__start__", "summarize", "agent", "precheck", "confirm", "tools", "__end__"}
        assert set(nodes) == expected

    def test_start_to_summarize(self):
        """START → summarize edge must exist."""
        graph = build_agent_graph()
        edges = graph.get_graph().edges
        start_edges = [e for e in edges if e.source == "__start__"]
        assert len(start_edges) == 1
        assert start_edges[0].target == "summarize"

    def test_summarize_node_present(self):
        """Graph should contain a summarize node that runs before agent."""
        graph = build_agent_graph()
        nodes = list(graph.get_graph().nodes.keys())
        assert "summarize" in nodes
        assert "agent" in nodes


class TestAgentGraphEndToEnd:
    """Integration-style tests with mocked LLM and MCP client."""

    @pytest.fixture
    def mock_deps(self):
        """Build a minimal set of mocked dependencies."""
        from langchain_core.messages import HumanMessage

        # Mock LLM manager — direct answer by default
        from langchain_core.messages import AIMessage
        llm_mgr = MagicMock()
        llm_response = AIMessage(content="Hello! I'm Athena. How can I help you?")

        mock_llm = MagicMock()
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)

        async def _mock_ainvoke(*args, **kwargs):
            return llm_response
        mock_llm.ainvoke = _mock_ainvoke
        type(llm_mgr).base_model = PropertyMock(return_value=mock_llm)
        llm_mgr.base_model_context_window = 128000
        llm_mgr.default_summarize_provider = None

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
        tool_result.content = {"result": "data"}

        async def _mock_call_tool(*args, **kwargs):
            return tool_result
        mcp_client.call_tool = _mock_call_tool
        mcp_client.export_for_llm = MagicMock(return_value=[])

        return {
            "llm_manager": llm_mgr,
            "harness_engine": harness,
            "mcp_client": mcp_client,
            "messages": [HumanMessage(content="hello")],
        }

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.summarize._load_session_summary", new_callable=AsyncMock, return_value=(None, 0))
    @patch("athena.core.graph.nodes.summarize._save_session_summary", new_callable=AsyncMock)
    @patch("athena.core.graph.nodes.agent._load_session_summary", new_callable=AsyncMock, return_value=None)
    async def test_direct_answer_flow(self, _mock_agent_summary, _mock_save_summary, _mock_load_summary, mock_deps):
        """Simple "hello" → LLM answers directly, no tool calls."""

        graph = build_agent_graph()

        initial_state = {
            "messages": mock_deps["messages"],
        }

        config = {
            "configurable": {
                "thread_id": "test-thread-1",
                "session_id": "test-session",
                "user_id": "test-user",
                "channel": "test",
                "llm_manager": mock_deps["llm_manager"],
                "mcp_client": mock_deps["mcp_client"],
                "harness_engine": mock_deps["harness_engine"],
            }
        }

        events = []
        async for event in graph.astream(
            initial_state,
            config=config,
            stream_mode="updates",
        ):
            events.append(event)

        # Should have agent node event, no tools node
        assert len(events) > 0, "Graph produced no events"
        agent_events = [e for e in events if "agent" in e]
        assert len(agent_events) == 1
        assert agent_events[0]["agent"]["status"] == "completed"

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.summarize._load_session_summary", new_callable=AsyncMock, return_value=(None, 0))
    @patch("athena.core.graph.nodes.summarize._save_session_summary", new_callable=AsyncMock)
    @patch("athena.core.graph.nodes.agent._load_session_summary", new_callable=AsyncMock, return_value=None)
    async def test_tool_calling_flow(self, _mock_agent_summary, _mock_save_summary, _mock_load_summary, mock_deps):
        """LLM requests tool → tools execute → agent synthesizes."""
        from langchain_core.messages import AIMessage, HumanMessage

        # Override LLM: first call returns tool_calls, second call returns answer
        call_count = [0]

        tool_response = AIMessage(content="Let me check the weather.")
        tool_response.tool_calls = [
            {"id": "call_1", "name": "weather", "args": {"city": "Tokyo"}}
        ]

        final_response = AIMessage(content="The weather in Tokyo is 22°C, sunny.")

        async def _mock_ainvoke(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return tool_response
            else:
                return final_response

        mock_llm = MagicMock()
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.ainvoke = _mock_ainvoke
        type(mock_deps["llm_manager"]).base_model = PropertyMock(return_value=mock_llm)

        # Add tool to registry
        mock_tool = MagicMock()
        mock_tool.source_server_id = "builtin-core"
        mock_tool.name = "weather"
        mock_deps["mcp_client"].get_tool_by_name = MagicMock(return_value=mock_tool)
        mock_deps["mcp_client"].export_for_llm.return_value = [
            {
                "type": "function",
                "function": {
                    "name": "weather",
                    "description": "Get weather for a city",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ]

        graph = build_agent_graph()

        initial_state = {
            "messages": [HumanMessage(content="What's the weather in Tokyo?")],
        }

        config = {
            "configurable": {
                "thread_id": "test-thread-2",
                "session_id": "test-session",
                "user_id": "test-user",
                "channel": "test",
                "llm_manager": mock_deps["llm_manager"],
                "mcp_client": mock_deps["mcp_client"],
                "harness_engine": mock_deps["harness_engine"],
            }
        }

        events = []
        async for event in graph.astream(
            initial_state,
            config=config,
            stream_mode="updates",
        ):
            events.append(event)

        # Should have: agent (tool_calls) → tools → agent (final answer)
        assert len(events) >= 2, f"Expected at least 2 events, got {len(events)}"
        node_types = [list(e.keys())[0] for e in events if not list(e.keys())[0].startswith("__")]
        assert "agent" in node_types
        assert "tools" in node_types

        # First agent event should have tool_calls
        agent_events = [e["agent"] for e in events if "agent" in e]
        assert agent_events[0]["status"] == "executing"
        assert agent_events[-1]["status"] == "completed"

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.summarize._load_session_summary", new_callable=AsyncMock, return_value=(None, 0))
    @patch("athena.core.graph.nodes.summarize._save_session_summary", new_callable=AsyncMock)
    @patch("athena.core.graph.nodes.agent._load_session_summary", new_callable=AsyncMock, return_value=None)
    async def test_agent_error_handling(self, _mock_agent_summary, _mock_save_summary, _mock_load_summary, mock_deps):
        """Graph handles LLM errors gracefully."""
        from langchain_core.messages import HumanMessage

        # Make LLM fail
        async def _mock_fail(*args, **kwargs):
            raise RuntimeError("LLM unavailable")

        mock_llm = MagicMock()
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.ainvoke = _mock_fail
        type(mock_deps["llm_manager"]).base_model = PropertyMock(return_value=mock_llm)

        graph = build_agent_graph()

        initial_state = {
            "messages": [HumanMessage(content="hello")],
        }

        config = {
            "configurable": {
                "thread_id": "test-thread-3",
                "session_id": "test-session",
                "user_id": "test-user",
                "channel": "test",
                "llm_manager": mock_deps["llm_manager"],
                "mcp_client": mock_deps["mcp_client"],
                "harness_engine": mock_deps["harness_engine"],
            }
        }

        events = []
        async for event in graph.astream(
            initial_state,
            config=config,
            stream_mode="updates",
        ):
            events.append(event)

        # Should have agent event with failed status
        agent_events = [e for e in events if "agent" in e]
        assert len(agent_events) > 0
        assert agent_events[0]["agent"]["status"] == "failed"

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.summarize._load_session_summary", new_callable=AsyncMock, return_value=(None, 0))
    @patch("athena.core.graph.nodes.summarize._save_session_summary", new_callable=AsyncMock)
    @patch("athena.core.graph.nodes.agent._load_session_summary", new_callable=AsyncMock, return_value=None)
    async def test_max_iterations_guard(self, _mock_agent_summary, _mock_save_summary, _mock_load_summary, mock_deps):
        """Graph should stop after MAX_AGENT_ITERATIONS tool-call loops."""
        from langchain_core.messages import AIMessage, HumanMessage
        from athena.core.graph.agent_routing import MAX_AGENT_ITERATIONS

        # LLM always returns tool_calls (simulate infinite loop scenario)
        always_tool_response = AIMessage(content="Let me check.")
        always_tool_response.tool_calls = [
            {"id": "call_1", "name": "weather", "args": {"city": "Tokyo"}}
        ]

        async def _always_tools(*args, **kwargs):
            return always_tool_response

        mock_llm = MagicMock()
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.ainvoke = _always_tools
        type(mock_deps["llm_manager"]).base_model = PropertyMock(return_value=mock_llm)

        mock_tool = MagicMock()
        mock_tool.source_server_id = "builtin-core"
        mock_tool.name = "weather"
        mock_deps["mcp_client"].get_tool_by_name = MagicMock(return_value=mock_tool)
        mock_deps["mcp_client"].export_for_llm.return_value = [
            {
                "type": "function",
                "function": {
                    "name": "weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        graph = build_agent_graph()

        initial_state = {
            "messages": [HumanMessage(content="weather please")],
        }

        config = {
            "configurable": {
                "thread_id": "test-thread-4",
                "session_id": "test-session",
                "user_id": "test-user",
                "channel": "test",
                "llm_manager": mock_deps["llm_manager"],
                "mcp_client": mock_deps["mcp_client"],
                "harness_engine": mock_deps["harness_engine"],
            }
        }

        # Just verify it doesn't hang — should stop due to max iterations
        events = []
        async for event in graph.astream(
            initial_state,
            config=config,
            stream_mode="updates",
        ):
            events.append(event)
            if len(events) > 100:  # Safety timeout (more than enough for 10 iterations)
                break

        # Should have at most MAX_AGENT_ITERATIONS agent invocations
        agent_events = [e for e in events if "agent" in e]
        assert len(agent_events) <= MAX_AGENT_ITERATIONS + 1

        # Last agent event should be the max-iterations fallback
        last_agent = agent_events[-1]["agent"]
        assert last_agent["status"] == "completed"
        assert "maximum number of iterations" in last_agent["messages"][0].content
