"""Tests for agent_node, confirm_node, tools_node, and summarize_node."""

import pytest
from unittest.mock import MagicMock, PropertyMock, patch, AsyncMock
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


# ── agent_node tests ────────────────────────────────────────────────────────


class TestAgentNode:
    """Tests for agent_node — LLM call with tools."""

    @pytest.fixture
    def mock_config(self):
        """Build a minimal configurable with mocked LLM + tool registry."""
        llm_mgr = MagicMock()
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock()
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        type(llm_mgr).base_model = PropertyMock(return_value=mock_llm)

        mcp_client = MagicMock()

        config = {
            "configurable": {
                "llm_manager": llm_mgr,
                "mcp_client": mcp_client,
                "tool_registry": None,
                "session_id": "test-session",
            }
        }
        return config, mock_llm

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.agent._load_tools", new_callable=AsyncMock, return_value=[MagicMock()])
    async def test_direct_answer_no_tools(self, _mock_tools, mock_config):
        """LLM returns text without tool_calls."""
        from athena.core.graph.nodes.agent import agent_node

        config, mock_llm = mock_config
        response = AIMessage(content="Hello!")
        response.tool_calls = []
        mock_llm.ainvoke.return_value = response

        state = {
            "messages": [HumanMessage(content="hello")],
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await agent_node(state, config)

        assert len(result["messages"]) == 1
        assert result["messages"][0].content == "Hello!"
        mock_llm.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.agent._load_tools", new_callable=AsyncMock, return_value=[MagicMock()])
    async def test_uses_effective_messages_when_present(self, _mock_tools, mock_config):
        """When effective_messages is in state, agent uses it directly."""
        from athena.core.graph.nodes.agent import agent_node

        config, mock_llm = mock_config
        response = AIMessage(content="Done")
        response.tool_calls = []
        mock_llm.ainvoke.return_value = response

        effective = [
            SystemMessage(content="You are Athena."),
            SystemMessage(content="Summary: user asked about weather"),
            HumanMessage(content="What about tomorrow?"),
        ]
        state = {
            "messages": [HumanMessage(content="hello"), HumanMessage(content="What about tomorrow?")],
            "effective_messages": effective,
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await agent_node(state, config)

        assert result["messages"][0].content == "Done"
        # Verify the LLM was called with effective_messages, not raw messages
        call_args = mock_llm.ainvoke.call_args[0][0]
        assert call_args is effective

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.agent._load_tools", new_callable=AsyncMock, return_value=[])
    async def test_falls_back_without_effective_messages(self, _mock_tools, mock_config):
        """Without effective_messages, agent builds its own message list."""
        from athena.core.graph.nodes.agent import agent_node

        config, mock_llm = mock_config
        response = AIMessage(content="OK")
        response.tool_calls = []
        mock_llm.ainvoke.return_value = response

        state = {
            "messages": [HumanMessage(content="hi")],
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await agent_node(state, config)

        call_args = mock_llm.ainvoke.call_args[0][0]
        # First message should be the system prompt
        assert isinstance(call_args[0], SystemMessage)
        assert "Athena" in call_args[0].content

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.agent._load_tools", new_callable=AsyncMock, return_value=[MagicMock()])
    async def test_tool_calls_passed_through(self, _mock_tools, mock_config):
        """LLM returns tool_calls — they appear in the response message."""
        from athena.core.graph.nodes.agent import agent_node

        config, mock_llm = mock_config
        response = AIMessage(content="")
        response.tool_calls = [
            {"id": "call_1", "name": "weather", "args": {"city": "Tokyo"}}
        ]
        mock_llm.ainvoke.return_value = response

        state = {
            "messages": [HumanMessage(content="weather?")],
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await agent_node(state, config)

        assert len(result["messages"]) == 1
        assert result["messages"][0].tool_calls[0]["name"] == "weather"

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.agent._load_tools", new_callable=AsyncMock, return_value=[])
    async def test_includes_system_context(self, _mock_tools, mock_config):
        """system_context from config is included as a system message."""
        from athena.core.graph.nodes.agent import agent_node

        config, mock_llm = mock_config
        config["configurable"]["system_context"] = "User prefers concise answers."

        response = AIMessage(content="Sure.")
        response.tool_calls = []
        mock_llm.ainvoke.return_value = response

        state = {
            "messages": [HumanMessage(content="hi")],
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        await agent_node(state, config)

        call_args = mock_llm.ainvoke.call_args[0][0]
        system_msgs = [m for m in call_args if isinstance(m, SystemMessage)]
        contexts = [m.content for m in system_msgs]
        assert any("concise" in c for c in contexts)


# ── confirm_node tests ──────────────────────────────────────────────────────


class TestConfirmNode:
    """Tests for confirm_node — harness check + sequential interrupt."""

    @pytest.fixture
    def mock_config(self):
        """Build a minimal configurable with mocked harness."""
        harness = MagicMock()
        harness_result = MagicMock()
        harness_result.allowed = True
        harness_result.requires_confirmation = False
        harness_result.risk_level = MagicMock()
        harness_result.risk_level.value = "low"
        harness_result.cooling_off_seconds = 0
        harness.pre_check = _async_return(harness_result)

        return {
            "configurable": {
                "harness_engine": harness,
                "session_id": "test-session",
            }
        }

    @pytest.mark.asyncio
    async def test_no_pending_calls(self, mock_config):
        """No pending calls → return empty confirmed list."""
        from athena.core.graph.nodes.confirm import confirm_node

        state = {
            "messages": [],
            "pending_tool_calls": None,
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await confirm_node(state, mock_config)
        assert result["confirmed_tool_calls"] is None
        assert result["pending_tool_calls"] is None

    @pytest.mark.asyncio
    async def test_allowed_tool_passes_through(self, mock_config):
        """Tool that passes harness check → confirmed without interrupt."""
        from athena.core.graph.nodes.confirm import confirm_node

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

        result = await confirm_node(state, mock_config)
        assert len(result["confirmed_tool_calls"]) == 1
        assert result["confirmed_tool_calls"][0]["name"] == "weather"
        assert result["pending_tool_calls"] is None

    @pytest.mark.asyncio
    async def test_blocked_tool_emits_error(self, mock_config):
        """Tool blocked by harness → error ToolMessage, not confirmed."""
        from athena.core.graph.nodes.confirm import confirm_node

        blocked_result = MagicMock()
        blocked_result.allowed = False
        blocked_result.requires_confirmation = False
        blocked_result.reason = "Access denied"
        mock_config["configurable"]["harness_engine"].pre_check = _async_return(blocked_result)

        state = {
            "messages": [],
            "pending_tool_calls": [
                {"id": "call_1", "name": "file_delete", "arguments": {"path": "/etc"}}
            ],
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await confirm_node(state, mock_config)
        assert result["confirmed_tool_calls"] is None
        assert len(result["messages"]) == 1
        assert "blocked" in result["messages"][0].content.lower()

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.confirm.interrupt")
    async def test_confirmation_tool_approved(self, mock_interrupt, mock_config):
        """Tool requiring confirmation → interrupt called, user approves."""
        from athena.core.graph.nodes.confirm import confirm_node

        confirm_result = MagicMock()
        confirm_result.allowed = True
        confirm_result.requires_confirmation = True
        confirm_result.risk_level = MagicMock()
        confirm_result.risk_level.value = "high"
        confirm_result.cooling_off_seconds = 0
        confirm_result.reason = ""
        mock_config["configurable"]["harness_engine"].pre_check = _async_return(confirm_result)

        mock_interrupt.return_value = "approved"

        state = {
            "messages": [],
            "pending_tool_calls": [
                {"id": "call_1", "name": "file_delete", "arguments": {"path": "/tmp"}}
            ],
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await confirm_node(state, mock_config)
        assert len(result["confirmed_tool_calls"]) == 1
        assert result["confirmed_tool_calls"][0]["name"] == "file_delete"

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.confirm.interrupt")
    async def test_confirmation_tool_rejected(self, mock_interrupt, mock_config):
        """Tool requiring confirmation → interrupt called, user rejects."""
        from athena.core.graph.nodes.confirm import confirm_node

        confirm_result = MagicMock()
        confirm_result.allowed = True
        confirm_result.requires_confirmation = True
        confirm_result.risk_level = MagicMock()
        confirm_result.risk_level.value = "high"
        confirm_result.cooling_off_seconds = 0
        confirm_result.reason = ""
        mock_config["configurable"]["harness_engine"].pre_check = _async_return(confirm_result)

        mock_interrupt.return_value = "rejected"

        state = {
            "messages": [],
            "pending_tool_calls": [
                {"id": "call_1", "name": "file_delete", "arguments": {"path": "/tmp"}}
            ],
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await confirm_node(state, mock_config)
        assert result["confirmed_tool_calls"] is None
        assert len(result["messages"]) == 1
        assert "rejected" in result["messages"][0].content.lower()


# ── tools_node tests ────────────────────────────────────────────────────────


class TestToolsNode:
    """Tests for tools_node — MCP tool execution (no harness/interrupt)."""

    @pytest.fixture
    def mock_config(self):
        """Build a minimal configurable with mocked MCP client."""
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
                "session_id": "test-session",
                "mcp_client": mcp_client,
                "tool_registry": tool_registry,
            }
        }

    @pytest.mark.asyncio
    async def test_executes_single_tool(self, mock_config):
        """Execute a single confirmed tool call successfully."""
        from athena.core.graph.nodes.tools import tools_node

        state = {
            "messages": [],
            "confirmed_tool_calls": [
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
    async def test_mcp_call_fails(self, mock_config):
        """MCP call returns failure → error ToolMessage."""
        from athena.core.graph.nodes.tools import tools_node

        failed_result = MagicMock()
        failed_result.success = False
        failed_result.error = "API rate limit exceeded"
        mock_config["configurable"]["mcp_client"].call_tool = _async_return(failed_result)

        state = {
            "messages": [],
            "confirmed_tool_calls": [
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
    async def test_no_confirmed_calls(self, mock_config):
        """Called with no confirmed calls → return empty dict."""
        from athena.core.graph.nodes.tools import tools_node

        state = {
            "messages": [],
            "confirmed_tool_calls": None,
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await tools_node(state, mock_config)
        assert result == {}

    @pytest.mark.asyncio
    async def test_mcp_call_exception(self, mock_config):
        """MCP call raises exception → error ToolMessage."""
        from athena.core.graph.nodes.tools import tools_node

        async def _raise(*args, **kwargs):
            raise RuntimeError("Connection timeout")
        mock_config["configurable"]["mcp_client"].call_tool = _raise

        state = {
            "messages": [],
            "confirmed_tool_calls": [
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


# ── summarize_node tests ────────────────────────────────────────────────────


class TestSummarizeNode:
    """Tests for summarize_node — context-aware summarisation."""

    @pytest.fixture
    def mock_config(self):
        llm_mgr = MagicMock()
        llm_mgr.base_model_context_window = 1000
        llm_mgr.default_summarize_provider = "test-fast"

        return {
            "configurable": {
                "llm_manager": llm_mgr,
                "session_id": "test-session",
                "sqlite_db_path": "",
            }
        }

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.summarize._load_session_summary", new_callable=AsyncMock)
    @patch("athena.core.graph.nodes.summarize.count_tokens_approximately", return_value=100)
    async def test_below_threshold_passthrough(self, _mock_count, mock_load, mock_config):
        """Token count below threshold → effective_messages built without summarisation."""
        from athena.core.graph.nodes.summarize import summarize_node

        mock_load.return_value = (None, 0)

        messages = [HumanMessage(content="hello")]
        state = {
            "messages": messages,
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await summarize_node(state, mock_config)

        assert "effective_messages" in result
        eff = result["effective_messages"]
        # First message should be system prompt
        assert isinstance(eff[0], SystemMessage)
        # Last should be the human message
        assert isinstance(eff[-1], HumanMessage)
        assert eff[-1].content == "hello"

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.summarize._save_session_summary", new_callable=AsyncMock)
    @patch("athena.core.graph.nodes.summarize._summarize_messages", new_callable=AsyncMock)
    @patch("athena.core.graph.nodes.summarize._load_session_summary", new_callable=AsyncMock)
    @patch("athena.core.graph.nodes.summarize.count_tokens_approximately")
    async def test_above_threshold_triggers_summarise(
        self, mock_count, mock_load, mock_summarise, mock_save, mock_config
    ):
        """Token count above threshold → summarise called, summary persisted."""
        from athena.core.graph.nodes.summarize import summarize_node

        mock_load.return_value = (None, 0)
        # Threshold is 1000 * 0.8 = 800.  Return 900 to trigger.
        mock_count.return_value = 900
        mock_summarise.return_value = "User said hello."

        messages = [
            HumanMessage(content="msg1"),
            HumanMessage(content="msg2"),
            HumanMessage(content="msg3"),
        ]
        state = {
            "messages": messages,
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await summarize_node(state, mock_config)

        mock_summarise.assert_awaited_once()
        mock_save.assert_awaited_once()
        eff = result["effective_messages"]
        # Should have system prompt + summary + last 2 messages
        summary_msgs = [m for m in eff if isinstance(m, SystemMessage) and "summary" in m.content.lower()]
        assert len(summary_msgs) >= 1

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.summarize._load_session_summary", new_callable=AsyncMock)
    @patch("athena.core.graph.nodes.summarize.count_tokens_approximately", return_value=100)
    async def test_existing_summary_included(self, _mock_count, mock_load, mock_config):
        """When session has a summary, it's included as a system message."""
        from athena.core.graph.nodes.summarize import summarize_node

        mock_load.return_value = ("Previous conversation about weather.", 2)

        messages = [
            HumanMessage(content="old1"),
            HumanMessage(content="old2"),
            HumanMessage(content="new msg"),
        ]
        state = {
            "messages": messages,
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await summarize_node(state, mock_config)

        eff = result["effective_messages"]
        summary_msgs = [m for m in eff if isinstance(m, SystemMessage) and "weather" in m.content]
        assert len(summary_msgs) == 1
        # Only messages after offset (2) should be included
        human_msgs = [m for m in eff if isinstance(m, HumanMessage)]
        assert len(human_msgs) == 1
        assert human_msgs[0].content == "new msg"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _async_return(value):
    """Create an async function that returns a fixed value."""
    async def _inner(*args, **kwargs):
        return value
    return _inner
