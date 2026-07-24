"""Tests for Tool Registry — state machine and fallback resolution."""

import pytest

from athena.mcp_client.client import MCPClient, ToolDef


class TestToolRegistry:
    """Test tool registration, state machine, and fallback."""

    @pytest.fixture
    def registry(self):
        from unittest.mock import MagicMock
        config = MagicMock()
        config.system.mcp_heartbeat_interval_seconds = 30
        config.system.mcp_reconnect_backoff_max_seconds = 60
        config.system.mcp_tools_list_refresh_on_reconnect = True
        return MCPClient(config)

    @pytest.fixture
    def sample_tools(self):
        return [
            ToolDef(name="web_search", description="Search the web",
                    capability_tags=["web_search", "text_retrieval"],
                    risk_level="low"),
            ToolDef(name="bing_search", description="Bing search",
                    capability_tags=["web_search"],
                    risk_level="low"),
            ToolDef(name="file_read", description="Read a file",
                    capability_tags=["filesystem", "read"],
                    risk_level="low"),
            ToolDef(name="file_write", description="Write a file",
                    capability_tags=["filesystem", "write"],
                    risk_level="medium"),
        ]

    @pytest.mark.asyncio
    async def test_register_tools(self, registry, sample_tools):
        """Registering tools should make them available."""
        ids = await registry.register_tools("builtin-core", sample_tools, "builtin")
        assert len(ids) == 4
        assert len(registry.get_active_tools()) == 4

    @pytest.mark.asyncio
    async def test_mark_stale(self, registry, sample_tools):
        """Marking server as stale should update all its tools."""
        await registry.register_tools("builtin-core", sample_tools, "builtin")
        await registry.mark_stale("builtin-core")
        active = registry.get_active_tools()
        assert len(active) == 0

    @pytest.mark.asyncio
    async def test_disable_tool(self, registry, sample_tools):
        """Admin disable should survive reconnects."""
        await registry.register_tools("builtin-core", sample_tools, "builtin")
        tool_id = list(registry._tools.keys())[0]
        await registry.disable_tool(tool_id)
        tool = registry.get_tool_by_id(tool_id)
        assert tool.status == "disabled"

    @pytest.mark.asyncio
    async def test_enable_tool(self, registry, sample_tools):
        """Re-enabling a disabled tool should work."""
        await registry.register_tools("builtin-core", sample_tools, "builtin")
        tool_id = list(registry._tools.keys())[0]
        await registry.disable_tool(tool_id)
        await registry.enable_tool(tool_id)
        tool = registry.get_tool_by_id(tool_id)
        assert tool.status == "active"

    @pytest.mark.asyncio
    async def test_fallback_same_capability_tag(self, registry, sample_tools):
        """Fallback should prefer tools with same capability_tag."""
        await registry.register_tools("builtin-core", sample_tools, "builtin")
        fallback = registry.resolve_fallback("web_search")
        assert fallback is not None
        # Should prefer bing_search because it shares web_search tag
        assert fallback.name == "bing_search"

    @pytest.mark.asyncio
    async def test_fallback_excludes_self(self, registry, sample_tools):
        """Fallback should never return the failed tool itself."""
        await registry.register_tools("builtin-core", sample_tools, "builtin")
        # Only web_search and bing_search have web_search tag
        # If web_search fails, bing_search is the only other option
        fallback = registry.resolve_fallback("web_search")
        assert fallback is not None
        assert fallback.name != "web_search"

    @pytest.mark.asyncio
    async def test_fallback_no_candidates(self, registry, sample_tools):
        """When no candidates exist, fallback should return None."""
        await registry.register_tools("builtin-core", sample_tools, "builtin")
        # Mark all as stale
        await registry.mark_stale("builtin-core")
        fallback = registry.resolve_fallback("web_search")
        assert fallback is None

    @pytest.mark.asyncio
    async def test_fallback_prefers_lower_risk(self, registry):
        """Fallback should prefer tools with lower risk_level."""
        tools = [
            ToolDef(name="tool_a", capability_tags=["tag1"], risk_level="critical"),
            ToolDef(name="tool_b", capability_tags=["tag1"], risk_level="low"),
        ]
        await registry.register_tools("srv1", tools, "external")
        fallback = registry.resolve_fallback("tool_a")
        assert fallback.name == "tool_b"  # Lower risk preferred

    def test_export_for_planner(self, registry, sample_tools):
        """Export should return only active tools in LLM-friendly format."""
        import asyncio
        asyncio.run(registry.register_tools("builtin-core", sample_tools, "builtin"))
        exported = registry.export_for_planner()
        assert len(exported) == 4
        for t in exported:
            assert "name" in t
            assert "description" in t
            assert "parameters_schema" in t
            assert "capability_tags" in t
