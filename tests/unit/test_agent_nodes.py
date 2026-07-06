"""Tests for agent_node and tools_node with mocked dependencies."""

import pytest
from unittest.mock import MagicMock, PropertyMock, patch, AsyncMock
from langchain_core.messages import AIMessage, HumanMessage


# ── agent_node tests ────────────────────────────────────────────────────────

class TestAgentNode:
    """Tests for agent_node — LLM call with tools."""

    @pytest.fixture
    def mock_config(self):
        """Build a minimal configurable with mocked LLM + tool registry."""
        llm_mgr = MagicMock()
        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock()
        type(llm_mgr).base_model = PropertyMock(return_value=mock_model)

        return {
            "configurable": {
                "llm_manager": llm_mgr,
                "mock_model": mock_model,  # expose for easy assertion
                "session_id": "test-session",
            }
        }

    @pytest.mark.asyncio
    @patch("athena.mcp_client.tool_loader.load_mcp_base_tools", new_callable=AsyncMock)
    async def test_direct_answer_no_tools(self, mock_load_tools, mock_config):
        """LLM returns text without tool_calls → completed status."""
        from athena.core.graph.nodes.agent import agent_node

        mock_load_tools.return_value = []

        # Mock LLM response: direct answer
        response = AIMessage(content="Hello! How can I help you today?")
        response.tool_calls = []
        mock_config["configurable"]["mock_model"].ainvoke.return_value = response

        state = {
            "messages": [HumanMessage(content="hello")],
            "context": None,
            "system_context": None,
            "pending_tool_calls": None,
            "agent_iteration": 0,
            "status": "thinking",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await agent_node(state, mock_config)

        assert result["status"] == "completed"
        assert result["pending_tool_calls"] is None
        assert result["agent_iteration"] == 1
        assert len(result["messages"]) == 1
        assert result["messages"][0].content == "Hello! How can I help you today?"

    @pytest.mark.asyncio
    @patch("athena.mcp_client.tool_loader.load_mcp_base_tools", new_callable=AsyncMock)
    async def test_tool_calls_requested(self, mock_load_tools, mock_config):
        """LLM returns tool_calls → executing status with pending calls."""
        from athena.core.graph.nodes.agent import agent_node

        mock_load_tools.return_value = []

        response = AIMessage(content="")
        response.tool_calls = [
            {"id": "call_1", "name": "weather", "args": {"city": "Tokyo"}}
        ]
        mock_config["configurable"]["mock_model"].ainvoke.return_value = response

        state = {
            "messages": [HumanMessage(content="What's the weather in Tokyo?")],
            "context": None,
            "system_context": None,
            "pending_tool_calls": None,
            "agent_iteration": 0,
            "status": "thinking",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await agent_node(state, mock_config)

        assert result["status"] == "executing"
        assert result["pending_tool_calls"] is not None
        assert len(result["pending_tool_calls"]) == 1
        assert result["pending_tool_calls"][0]["name"] == "weather"

    @pytest.mark.asyncio
    @patch("athena.mcp_client.tool_loader.load_mcp_base_tools", new_callable=AsyncMock)
    async def test_text_with_tool_calls(self, mock_load_tools, mock_config):
        """LLM returns both text and tool_calls (e.g., "Let me check...")."""
        from athena.core.graph.nodes.agent import agent_node

        mock_load_tools.return_value = []

        response = AIMessage(content="Let me look up the weather for you.")
        response.tool_calls = [
            {"id": "call_1", "name": "weather", "args": {"city": "Tokyo"}}
        ]
        mock_config["configurable"]["mock_model"].ainvoke.return_value = response

        state = {
            "messages": [HumanMessage(content="weather in Tokyo?")],
            "system_context": None,
            "pending_tool_calls": None,
            "agent_iteration": 0,
            "status": "thinking",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await agent_node(state, mock_config)

        assert result["status"] == "executing"
        assert result["pending_tool_calls"] is not None
        # Should have text content AND tool_calls on the message
        assert result["messages"][0].content == "Let me look up the weather for you."

    @pytest.mark.asyncio
    @patch("athena.mcp_client.tool_loader.load_mcp_base_tools", new_callable=AsyncMock)
    async def test_llm_error_returns_failed(self, mock_load_tools, mock_config):
        """LLM call fails → failed status with error message."""
        from athena.core.graph.nodes.agent import agent_node

        mock_load_tools.return_value = []

        async def _fail(*args, **kwargs):
            raise RuntimeError("LLM API down")
        mock_config["configurable"]["mock_model"].ainvoke = _fail

        state = {
            "messages": [HumanMessage(content="hello")],
            "context": None,
            "system_context": None,
            "pending_tool_calls": None,
            "agent_iteration": 0,
            "status": "thinking",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await agent_node(state, mock_config)

        assert result["status"] == "failed"
        assert result["pending_tool_calls"] is None
        assert "error" in result["messages"][0].content.lower()

    @pytest.mark.asyncio
    @patch("athena.mcp_client.tool_loader.load_mcp_base_tools", new_callable=AsyncMock)
    async def test_includes_system_context(self, mock_load_tools, mock_config):
        """System context should be included as a second system message."""
        from athena.core.graph.nodes.agent import agent_node

        mock_load_tools.return_value = []

        response = AIMessage(content="I see. Let me help with that.")
        response.tool_calls = []
        mock_config["configurable"]["mock_model"].ainvoke.return_value = response

        state = {
            "messages": [HumanMessage(content="help")],
            "system_context": "Previous: user asked about weather.",
            "pending_tool_calls": None,
            "agent_iteration": 0,
            "status": "thinking",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await agent_node(state, mock_config)
        assert result["status"] == "completed"


# ── tools_node tests ────────────────────────────────────────────────────────

class TestToolsNode:
    """Tests for tools_node — MCP tool execution pipeline."""

    @pytest.fixture
    def mock_config(self):
        """Build a minimal configurable with mocked harness + MCP client."""
        harness = MagicMock()
        harness_result = MagicMock()
        harness_result.allowed = True
        harness_result.requires_confirmation = False
        harness_result.risk_level = MagicMock()
        harness_result.risk_level.value = "low"
        harness_result.cooling_off_seconds = 0
        harness.pre_check = _async_return(harness_result)

        mcp_client = MagicMock()
        mcp_result = MagicMock()
        mcp_result.success = True
        mcp_result.content = {"temperature": 22, "condition": "sunny"}
        mcp_client.call_tool = _async_return(mcp_result)

        tool_registry = MagicMock()
        mock_tool = MagicMock()
        mock_tool.source_server_id = "builtin-core"
        tool_registry.get_tool_by_name.return_value = mock_tool

        return {
            "configurable": {
                "harness_engine": harness,
                "mcp_client": mcp_client,
                "tool_registry": tool_registry,
            }
        }

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.tools._load_mcp_tools_map", new_callable=AsyncMock)
    async def test_executes_single_tool(self, mock_load_tools, mock_config):
        """Execute a single pending tool call successfully."""
        from athena.core.graph.nodes.tools import tools_node

        mock_load_tools.return_value = {}  # No MCP tools, use fallback

        # Send() passes a single tool call per branch
        state = {
            "messages": [],
            "pending_tool_calls": [
                {"id": "call_1", "name": "weather", "arguments": {"city": "Tokyo"}}
            ],
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await tools_node(state, mock_config)

        assert len(result["messages"]) == 1
        msg = result["messages"][0]
        assert msg.tool_call_id == "call_1"
        assert msg.name == "weather"
        assert "temperature" in msg.content

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.tools._load_mcp_tools_map", new_callable=AsyncMock)
    async def test_harness_blocked(self, mock_load_tools, mock_config):
        """Harness blocks the tool → error ToolMessage."""
        from athena.core.graph.nodes.tools import tools_node

        mock_load_tools.return_value = {}

        # Override harness to block
        blocked_result = MagicMock()
        blocked_result.allowed = False
        blocked_result.requires_confirmation = False
        blocked_result.reason = "Access denied"
        mock_config["configurable"]["harness_engine"].pre_check = _async_return(
            blocked_result
        )

        state = {
            "messages": [],
            "pending_tool_calls": [
                {"id": "call_1", "name": "file_delete", "arguments": {"path": "/etc/hosts"}}
            ],
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await tools_node(state, mock_config)

        assert len(result["messages"]) == 1
        assert "error" in result["messages"][0].content.lower()
        assert "blocked" in result["messages"][0].content.lower()

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.tools._load_mcp_tools_map", new_callable=AsyncMock)
    async def test_mcp_call_fails(self, mock_load_tools, mock_config):
        """MCP call returns failure → error ToolMessage."""
        from athena.core.graph.nodes.tools import tools_node

        mock_load_tools.return_value = {}

        failed_result = MagicMock()
        failed_result.success = False
        failed_result.error = "API rate limit exceeded"
        mock_config["configurable"]["mcp_client"].call_tool = _async_return(failed_result)

        state = {
            "messages": [],
            "pending_tool_calls": [
                {"id": "call_1", "name": "weather", "arguments": {"city": "Tokyo"}}
            ],
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await tools_node(state, mock_config)

        assert len(result["messages"]) == 1
        assert "error" in result["messages"][0].content.lower()

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.tools._load_mcp_tools_map", new_callable=AsyncMock)
    async def test_no_pending_calls(self, mock_load_tools, mock_config):
        """Called with no pending calls → return empty dict."""
        from athena.core.graph.nodes.tools import tools_node

        mock_load_tools.return_value = {}

        state = {
            "messages": [],
            "pending_tool_calls": None,
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await tools_node(state, mock_config)
        assert result == {}

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.tools._load_mcp_tools_map", new_callable=AsyncMock)
    async def test_mcp_call_exception(self, mock_load_tools, mock_config):
        """MCP call raises exception → error ToolMessage."""
        from athena.core.graph.nodes.tools import tools_node

        mock_load_tools.return_value = {}

        async def _raise(*args, **kwargs):
            raise RuntimeError("Connection timeout")
        mock_config["configurable"]["mcp_client"].call_tool = _raise

        state = {
            "messages": [],
            "pending_tool_calls": [
                {"id": "call_1", "name": "weather", "arguments": {"city": "Tokyo"}}
            ],
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await tools_node(state, mock_config)

        assert len(result["messages"]) == 1
        assert "error" in result["messages"][0].content.lower()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _async_return(value):
    """Create an async function that returns a fixed value."""
    async def _inner(*args, **kwargs):
        return value
    return _inner
