"""Tests for agent_node, precheck_node, confirm_node, tools_node, and summarize_node."""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from athena.core.graph.nodes.precheck import AgentState

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
                "session_id": "test-session",
            }
        }
        return config, mock_llm

    @pytest.mark.asyncio
    @patch(
        "athena.core.graph.nodes.agent._load_session_summary",
        new_callable=AsyncMock,
        return_value=None,
    )
    @patch(
        "athena.core.graph.nodes.agent._load_tools",
        new_callable=AsyncMock,
        return_value=[MagicMock()],
    )
    async def test_direct_answer_no_tools(self, _mock_tools, _mock_summary, mock_config):
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

        result = await agent_node(AgentState(**state), config)

        assert len(result["messages"]) == 1
        assert result["messages"][0].content == "Hello!"
        mock_llm.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    @patch(
        "athena.core.graph.nodes.agent._load_session_summary",
        new_callable=AsyncMock,
        return_value=None,
    )
    @patch(
        "athena.core.graph.nodes.agent._load_tools",
        new_callable=AsyncMock,
        return_value=[MagicMock()],
    )
    async def test_uses_summary_offset(self, _mock_tools, _mock_summary, mock_config):
        """When summary_offset is set, agent slices messages from that offset."""
        from athena.core.graph.nodes.agent import agent_node

        config, mock_llm = mock_config
        response = AIMessage(content="Done")
        response.tool_calls = []
        mock_llm.ainvoke.return_value = response

        state = {
            "messages": [HumanMessage(content="old"), HumanMessage(content="What about tomorrow?")],
            "summary_offset": 1,
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await agent_node(AgentState(**state), config)

        assert result["messages"][0].content == "Done"
        call_kwargs = mock_llm.ainvoke.call_args.kwargs
        assert "input" in call_kwargs
        call_args = call_kwargs["input"]
        assert isinstance(call_args[0], SystemMessage)
        human_msgs = [m for m in call_args if isinstance(m, HumanMessage)]
        assert len(human_msgs) == 1
        assert human_msgs[0].content == "What about tomorrow?"

    @pytest.mark.asyncio
    @patch(
        "athena.core.graph.nodes.agent._load_session_summary",
        new_callable=AsyncMock,
        return_value=None,
    )
    @patch("athena.core.graph.nodes.agent._load_tools", new_callable=AsyncMock, return_value=[])
    @patch(
        "athena.core.graph.nodes.agent.AGENT_SYSTEM_PROMPT", "Athena is your name. You are helpful."
    )
    async def test_builds_message_list(self, _mock_tools, _mock_summary, mock_config):
        """Agent always builds message list with system prompt from raw messages."""
        from athena.core.graph.nodes.agent import agent_node, _build_messages

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

        result = await agent_node(AgentState(**state), config)

        call_kwargs = mock_llm.ainvoke.call_args.kwargs
        assert "input" in call_kwargs
        call_args = call_kwargs["input"]
        assert isinstance(call_args[0], SystemMessage)
        assert "Athena" in call_args[0].content

    @pytest.mark.asyncio
    @patch(
        "athena.core.graph.nodes.agent._load_session_summary",
        new_callable=AsyncMock,
        return_value=None,
    )
    @patch(
        "athena.core.graph.nodes.agent._load_tools",
        new_callable=AsyncMock,
        return_value=[MagicMock()],
    )
    async def test_tool_calls_passed_through(self, _mock_tools, _mock_summary, mock_config):
        """LLM returns tool_calls — they appear in the response message."""
        from athena.core.graph.nodes.agent import agent_node

        config, mock_llm = mock_config
        response = AIMessage(content="")
        response.tool_calls = [{"id": "call_1", "name": "weather", "args": {"city": "Tokyo"}}]
        mock_llm.ainvoke.return_value = response

        state = {
            "messages": [HumanMessage(content="weather?")],
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await agent_node(AgentState(**state), config)

        assert len(result["messages"]) == 1
        assert result["messages"][0].tool_calls[0]["name"] == "weather"


# ── precheck_node tests ─────────────────────────────────────────────────────


class TestPrecheckNode:
    """Tests for precheck_node — harness pre-check without interrupt."""

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
        """No pending calls → return empty results."""
        from athena.core.graph.nodes.precheck import precheck_node

        state = {
            "messages": [],
            "pending_tool_calls": None,
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await precheck_node(AgentState(**state), mock_config)
        assert result["allowed_tool_calls"] is None
        assert result["blocked_tool_calls"] is None
        assert result["needs_confirmation_tool_calls"] is None
        assert result["pending_tool_calls"] is None

    @pytest.mark.asyncio
    async def test_allowed_tool(self, mock_config):
        """Tool that passes harness → allowed_tool_calls."""
        from athena.core.graph.nodes.precheck import precheck_node

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

        result = await precheck_node(AgentState(**state), mock_config)
        assert len(result["allowed_tool_calls"]) == 1
        assert result["allowed_tool_calls"][0]["name"] == "weather"
        assert result["blocked_tool_calls"] is None
        assert result["needs_confirmation_tool_calls"] is None
        assert result["pending_tool_calls"] is None

    @pytest.mark.asyncio
    async def test_blocked_tool(self, mock_config):
        """Tool blocked by harness → error message + blocked_tool_calls."""
        from athena.core.graph.nodes.precheck import precheck_node

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

        result = await precheck_node(AgentState(**state), mock_config)
        assert result["allowed_tool_calls"] is None
        assert len(result["blocked_tool_calls"]) == 1
        assert result["needs_confirmation_tool_calls"] is None
        assert len(result["messages"]) == 1
        assert "blocked" in result["messages"][0].content.lower()

    @pytest.mark.asyncio
    async def test_needs_confirmation(self, mock_config):
        """Tool requiring confirmation → needs_confirmation_tool_calls."""
        from athena.core.graph.nodes.precheck import precheck_node

        confirm_result = MagicMock()
        confirm_result.allowed = True
        confirm_result.requires_confirmation = True
        confirm_result.risk_level = MagicMock()
        confirm_result.risk_level.value = "high"
        confirm_result.cooling_off_seconds = 0
        confirm_result.reason = ""
        mock_config["configurable"]["harness_engine"].pre_check = _async_return(confirm_result)

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

        result = await precheck_node(AgentState(**state), mock_config)
        assert result["allowed_tool_calls"] is None
        assert result["blocked_tool_calls"] is None
        assert len(result["needs_confirmation_tool_calls"]) == 1
        assert result["needs_confirmation_tool_calls"][0]["name"] == "file_delete"

    @pytest.mark.asyncio
    async def test_mixed_classifications(self, mock_config):
        """Multiple tool calls classified into different categories."""
        from athena.core.graph.nodes.precheck import precheck_node

        call_count = [0]
        results_by_call = [
            MagicMock(allowed=True, requires_confirmation=False),
            MagicMock(allowed=False, requires_confirmation=False, reason="Blocked"),
            MagicMock(
                allowed=True,
                requires_confirmation=True,
                risk_level=MagicMock(value="high"),
                cooling_off_seconds=0,
                reason="",
            ),
        ]

        async def _mock_pre_check(*args, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return results_by_call[idx]

        mock_config["configurable"]["harness_engine"].pre_check = _mock_pre_check

        state = {
            "messages": [],
            "pending_tool_calls": [
                {"id": "call_1", "name": "weather", "arguments": {"city": "Tokyo"}},
                {"id": "call_2", "name": "file_delete", "arguments": {"path": "/etc"}},
                {"id": "call_3", "name": "file_write", "arguments": {"path": "/tmp"}},
            ],
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await precheck_node(AgentState(**state), mock_config)
        assert len(result["allowed_tool_calls"]) == 1
        assert result["allowed_tool_calls"][0]["name"] == "weather"
        assert len(result["blocked_tool_calls"]) == 1
        assert result["blocked_tool_calls"][0]["name"] == "file_delete"
        assert len(result["needs_confirmation_tool_calls"]) == 1
        assert result["needs_confirmation_tool_calls"][0]["name"] == "file_write"
        assert result["pending_tool_calls"] is None


# ── confirm_node tests ──────────────────────────────────────────────────────


class TestConfirmNode:
    """Tests for confirm_node — sequential interrupt for pre-classified calls."""

    @pytest.fixture
    def mock_config(self):
        """Build a minimal configurable (no harness needed — precheck handles that)."""
        return {
            "configurable": {
                "session_id": "test-session",
            }
        }

    @pytest.mark.asyncio
    async def test_no_needs_confirmation(self, mock_config):
        """No needs_confirmation calls → return empty confirmed list."""
        from athena.core.graph.nodes.confirm import confirm_node

        state = {
            "messages": [],
            "needs_confirmation_tool_calls": None,
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await confirm_node(AgentState(**state), mock_config)
        assert result["confirmed_tool_calls"] is None
        assert result["needs_confirmation_tool_calls"] is None

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.confirm.interrupt")
    async def test_confirmation_tool_approved(self, mock_interrupt, mock_config):
        """Tool requiring confirmation → interrupt called, user approves."""
        from athena.core.graph.nodes.confirm import confirm_node

        mock_interrupt.return_value = "approved"

        state = {
            "messages": [],
            "needs_confirmation_tool_calls": [
                {
                    "id": "call_1",
                    "name": "file_delete",
                    "arguments": {"path": "/tmp"},
                    "_harness_risk_level": "high",
                    "_harness_cooling_off": 0,
                    "_harness_reason": "",
                }
            ],
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await confirm_node(AgentState(**state), mock_config)
        assert len(result["confirmed_tool_calls"]) == 1
        assert result["confirmed_tool_calls"][0]["name"] == "file_delete"
        assert result["needs_confirmation_tool_calls"] is None

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.confirm.interrupt")
    async def test_confirmation_tool_rejected(self, mock_interrupt, mock_config):
        """Tool requiring confirmation → interrupt called, user rejects."""
        from athena.core.graph.nodes.confirm import confirm_node

        mock_interrupt.return_value = "rejected"

        state = {
            "messages": [],
            "needs_confirmation_tool_calls": [
                {
                    "id": "call_1",
                    "name": "file_delete",
                    "arguments": {"path": "/tmp"},
                    "_harness_risk_level": "high",
                    "_harness_cooling_off": 0,
                    "_harness_reason": "",
                }
            ],
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await confirm_node(AgentState(**state), mock_config)
        assert result["confirmed_tool_calls"] is None
        assert len(result["messages"]) == 1
        assert "rejected" in result["messages"][0].content.lower()
        assert result["needs_confirmation_tool_calls"] is None

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.confirm.interrupt")
    async def test_multiple_tools_first_call(self, mock_interrupt, mock_config):
        """Multiple tools requiring confirmation → first call processes only first tool."""
        from athena.core.graph.nodes.confirm import confirm_node

        mock_interrupt.return_value = "approved"

        state = {
            "messages": [],
            "needs_confirmation_tool_calls": [
                {
                    "id": "call_1",
                    "name": "file_delete",
                    "arguments": {"path": "/tmp"},
                    "_harness_risk_level": "high",
                    "_harness_cooling_off": 0,
                    "_harness_reason": "",
                },
                {
                    "id": "call_2",
                    "name": "file_write",
                    "arguments": {"path": "/tmp/out"},
                    "_harness_risk_level": "medium",
                    "_harness_cooling_off": 0,
                    "_harness_reason": "",
                },
            ],
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await confirm_node(AgentState(**state), mock_config)
        # First call processes only the first tool
        assert len(result["confirmed_tool_calls"]) == 1
        assert result["confirmed_tool_calls"][0]["name"] == "file_delete"
        assert mock_interrupt.call_count == 1
        # Second tool remains in needs_confirmation for next cycle
        assert len(result["needs_confirmation_tool_calls"]) == 1
        assert result["needs_confirmation_tool_calls"][0]["name"] == "file_write"

    @pytest.mark.asyncio
    @patch("athena.core.graph.nodes.confirm.interrupt")
    async def test_carries_forward_confirmed_tools(self, mock_interrupt, mock_config):
        """Confirmed tools from previous cycles are carried forward correctly."""
        from athena.core.graph.nodes.confirm import confirm_node

        mock_interrupt.return_value = "approved"

        # Simulate second cycle: first tool already confirmed, second tool needs confirmation
        state = {
            "messages": [],
            "confirmed_tool_calls": [
                {"id": "call_1", "name": "file_delete", "arguments": {"path": "/tmp"}}
            ],
            "needs_confirmation_tool_calls": [
                {
                    "id": "call_2",
                    "name": "file_write",
                    "arguments": {"path": "/tmp/out"},
                    "_harness_risk_level": "medium",
                    "_harness_cooling_off": 0,
                    "_harness_reason": "",
                },
            ],
            "agent_iteration": 1,
            "status": "executing",
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await confirm_node(AgentState(**state), mock_config)
        # Only the second tool should be interrupted
        assert mock_interrupt.call_count == 1
        # Both tools should be in confirmed list (carried forward + new)
        assert len(result["confirmed_tool_calls"]) == 2
        assert result["confirmed_tool_calls"][0]["name"] == "file_delete"
        assert result["confirmed_tool_calls"][1]["name"] == "file_write"
        # No more tools need confirmation
        assert result["needs_confirmation_tool_calls"] is None


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

        mock_tool = MagicMock()
        mock_tool.source_server_id = "builtin-core"
        mcp_client.get_tool_by_name = MagicMock(return_value=mock_tool)

        return {
            "configurable": {
                "session_id": "test-session",
                "mcp_client": mcp_client,
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

        result = await tools_node(AgentState(**state), mock_config)

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

        result = await tools_node(AgentState(**state), mock_config)

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

        result = await tools_node(AgentState(**state), mock_config)
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

        result = await tools_node(AgentState(**state), mock_config)

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
        """Token count below threshold → effective_messages contains raw conversation messages only."""
        from athena.core.graph.nodes.summarize import summarize_node

        mock_load.return_value = (None, 0)

        messages = [HumanMessage(content="hello")]
        state = {
            "messages": messages,
            "session_id": "test",
            "user_id": "user1",
            "channel": "web",
        }

        result = await summarize_node(AgentState(**state), mock_config)

        assert "summary_offset" in result
        assert "effective_messages" not in result
        assert result["summary_offset"] == 0

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

        result = await summarize_node(AgentState(**state), mock_config)

        mock_summarise.assert_awaited_once()
        mock_save.assert_awaited_once()
        assert "effective_messages" not in result
        assert result["summary_offset"] > 0

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

        result = await summarize_node(AgentState(**state), mock_config)

        assert "effective_messages" not in result
        assert result["summary_offset"] == 2


# ── Helpers ──────────────────────────────────────────────────────────────────


def _async_return(value):
    """Create an async function that returns a fixed value."""

    async def _inner(*args, **kwargs):
        return value

    return _inner
