"""Tests for agent graph construction and end-to-end execution."""

import pytest
from unittest.mock import MagicMock

from athena.core.graph import build_agent_graph


class TestAgentGraphConstruction:
    """Verify that the agent graph compiles with correct structure."""

    def test_graph_compiles(self):
        """Graph must compile without errors."""
        graph = build_agent_graph()
        assert graph is not None

    def test_nodes_present(self):
        """Agent and tools nodes must be registered."""
        graph = build_agent_graph()
        nodes = list(graph.get_graph().nodes.keys())
        expected = {"__start__", "agent", "tools", "__end__"}
        assert set(nodes) == expected

    def test_start_to_agent(self):
        """START → agent edge must exist (without summarization model)."""
        graph = build_agent_graph()
        edges = graph.get_graph().edges
        start_edges = [e for e in edges if e.source == "__start__"]
        assert len(start_edges) == 1
        assert start_edges[0].target == "agent"

    def test_summarize_node_absent_without_model(self):
        """Without summarization_model, no summarize node should be present."""
        graph = build_agent_graph()
        nodes = list(graph.get_graph().nodes.keys())
        assert "summarize" not in nodes

    def test_summarize_node_present_with_model(self):
        """With summarization_model, summarize node should appear."""
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(model="deepseek-v4-flash", base_url="https://api.deepseek.com/v1", api_key="test")
        graph = build_agent_graph(summarization_model=model)
        nodes = list(graph.get_graph().nodes.keys())
        assert "summarize" in nodes

        # START → summarize → agent
        edges = graph.get_graph().edges
        start_edges = [e for e in edges if e.source == "__start__"]
        assert start_edges[0].target == "summarize"


class TestAgentGraphEndToEnd:
    """Integration-style tests with mocked LLM and MCP client."""

    @pytest.fixture
    def mock_deps(self):
        """Build a minimal set of mocked dependencies."""
        from langchain_core.messages import HumanMessage

        # Mock LLM manager — direct answer by default
        llm_mgr = MagicMock()
        llm_response = MagicMock()
        llm_response.text = "Hello! I'm Athena. How can I help you?"
        llm_response.tool_calls = []

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
        tool_result.content = {"result": "data"}

        async def _mock_call_tool(*args, **kwargs):
            return tool_result
        mcp_client.call_tool = _mock_call_tool

        # Mock tool registry
        tool_registry = MagicMock()
        tool_registry.export_for_llm.return_value = []

        return {
            "llm_manager": llm_mgr,
            "harness_engine": harness,
            "mcp_client": mcp_client,
            "tool_registry": tool_registry,
            "messages": [HumanMessage(content="hello")],
        }

    @pytest.mark.asyncio
    async def test_direct_answer_flow(self, mock_deps):
        """Simple "hello" → LLM answers directly, no tool calls."""

        graph = build_agent_graph()

        initial_state = {
            "messages": mock_deps["messages"],
            "system_context": None,
            "context": None,
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

        # Should have agent node event, no tools node
        assert len(events) > 0, "Graph produced no events"
        agent_events = [e for e in events if "agent" in e]
        assert len(agent_events) == 1
        assert agent_events[0]["agent"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_tool_calling_flow(self, mock_deps):
        """LLM requests tool → tools execute → agent synthesizes."""
        from langchain_core.messages import HumanMessage

        # Override LLM: first call returns tool_calls, second call returns answer
        call_count = [0]

        tool_response = MagicMock()
        tool_response.text = "Let me check the weather."
        tool_response.tool_calls = [
            {"id": "call_1", "name": "weather", "arguments": {"city": "Tokyo"}}
        ]

        final_response = MagicMock()
        final_response.text = "The weather in Tokyo is 22°C, sunny."
        final_response.tool_calls = []

        async def _mock_generate(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Check that tools were passed
                return tool_response
            else:
                return final_response

        mock_deps["llm_manager"].generate = _mock_generate

        # Add tool to registry
        mock_tool = MagicMock()
        mock_tool.source_server_id = "builtin-core"
        mock_tool.name = "weather"
        mock_deps["tool_registry"].get_tool_by_name.return_value = mock_tool
        mock_deps["tool_registry"].export_for_llm.return_value = [
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
            "system_context": None,
            "context": None,
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
    async def test_agent_error_handling(self, mock_deps):
        """Graph handles LLM errors gracefully."""
        from langchain_core.messages import HumanMessage

        # Make LLM fail
        async def _mock_fail(*args, **kwargs):
            raise RuntimeError("LLM unavailable")
        mock_deps["llm_manager"].generate = _mock_fail

        graph = build_agent_graph()

        initial_state = {
            "messages": [HumanMessage(content="hello")],
            "system_context": None,
            "context": None,
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

        # Should have agent event with failed status
        agent_events = [e for e in events if "agent" in e]
        assert len(agent_events) > 0
        assert agent_events[0]["agent"]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_max_iterations_guard(self, mock_deps):
        """Graph should stop after MAX_AGENT_ITERATIONS tool-call loops."""
        from langchain_core.messages import HumanMessage
        from athena.core.graph.agent_routing import MAX_AGENT_ITERATIONS

        # LLM always returns tool_calls (simulate infinite loop scenario)
        tool_response = MagicMock()
        tool_response.text = ""
        tool_response.tool_calls = [
            {"id": "call_1", "name": "weather", "arguments": {"city": "Tokyo"}}
        ]

        async def _always_tools(*args, **kwargs):
            return tool_response
        mock_deps["llm_manager"].generate = _always_tools

        mock_tool = MagicMock()
        mock_tool.source_server_id = "builtin-core"
        mock_tool.name = "weather"
        mock_deps["tool_registry"].get_tool_by_name.return_value = mock_tool
        mock_deps["tool_registry"].export_for_llm.return_value = [
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
            "system_context": None,
            "context": None,
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
                "tool_registry": mock_deps["tool_registry"],
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
            if len(events) > 50:  # Safety timeout
                break

        # Should have at most MAX_AGENT_ITERATIONS agent invocations
        agent_events = [e for e in events if "agent" in e]
        assert len(agent_events) <= MAX_AGENT_ITERATIONS + 1
